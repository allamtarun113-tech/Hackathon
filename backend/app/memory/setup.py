"""CLI that configures the Hindsight bank before any data is loaded.

pagermind-bank apply      # create/converge bank config, directives and webhook, then verify
pagermind-bank verify     # read back what Hindsight is actually running (Memory Defense, observations, ...)
pagermind-bank show       # print the configuration that would be applied
pagermind-bank audit      # read-only: compare the bank's documents, directives and webhook with expectations
pagermind-bank prune-legacy [--apply]   # delete documents PagerMind did not write (dry run by default)
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any

import typer
import yaml

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.importers.source import SeedSource
from app.memory.audit import BankAudit, audit_bank
from app.memory.bank import MEMORY_DEFENSE, MEMORY_DEFENSE_RULES, BankConfig, entity_labels
from app.memory.client import HindsightGateway
from app.memory.documents import incident_document_id

app = typer.Typer(help="Configure the PagerMind Hindsight memory bank.", no_args_is_help=True)
log = get_logger(__name__)


def build_config(bank_id: str) -> BankConfig:
    settings = get_settings()
    catalog = yaml.safe_load((settings.data_dir / "service_catalog.yaml").read_text())
    services = list(catalog["services"])
    teams = sorted({service["team"] for service in catalog["services"].values()})
    return BankConfig(bank_id=bank_id, entity_labels=entity_labels(services, teams))


def verification_report(resolved: dict[str, Any]) -> dict[str, bool]:
    """What matters for retrieval quality and safety, read from the bank's resolved configuration."""
    config = resolved.get("config", resolved) or {}
    defense = config.get("memory_defense") or {}
    return {
        "memory_defense": bool(defense) and defense.get("enabled", True) is not False,
        "observations": bool(config.get("enable_observations")),
        "verbose_extraction": config.get("retain_extraction_mode") == "verbose",
        "entity_labels": bool(config.get("entity_labels")),
    }


async def _verify(gateway: HindsightGateway, *, require_memory_defense: bool) -> None:
    report = verification_report(await gateway.bank_config())
    for check, ok in report.items():
        typer.echo(f"{'OK  ' if ok else 'MISSING'}  {check}")
    missing = [check for check, ok in report.items() if not ok and check != "memory_defense"]
    if not report["memory_defense"] and require_memory_defense:
        missing.append("memory_defense")
    if missing:
        typer.echo(
            f"Bank {gateway.bank_id} is not configured as PagerMind expects: {', '.join(missing)}. "
            "If your Hindsight plan does not include Memory Defense, re-run with --no-require-memory-defense "
            "(app-side guardrails still apply).",
            err=True,
        )
        raise typer.Exit(code=2)


async def _apply(bank_id: str, register_webhook: bool, require_memory_defense: bool) -> None:
    settings = get_settings()
    gateway = HindsightGateway(settings, bank_id=bank_id)
    try:
        detectors = await gateway.apply_bank_config(build_config(bank_id), memory_defense=MEMORY_DEFENSE)
        wanted = [rule["on"] for rule in MEMORY_DEFENSE_RULES]
        missing = [detector for detector in wanted if detector not in detectors]
        log.info("bank_configured", bank_id=bank_id, memory_defense_detectors=detectors)
        if missing:
            typer.echo(
                f"Memory Defense detectors not on this Hindsight plan: {', '.join(missing)}. "
                "PagerMind's own input and memory guardrails still screen that content.",
                err=True,
            )
        if register_webhook:
            if not settings.hindsight_webhook_url or not settings.hindsight_webhook_secret.get_secret_value():
                raise typer.BadParameter("HINDSIGHT_WEBHOOK_URL and HINDSIGHT_WEBHOOK_SECRET must be set.")
            webhook_id = await gateway.ensure_webhook(
                url=settings.hindsight_webhook_url,
                secret=settings.hindsight_webhook_secret.get_secret_value(),
            )
            log.info("webhook_registered", webhook_id=webhook_id)
        await _verify(gateway, require_memory_defense=require_memory_defense)
    finally:
        await gateway.close()


async def _verify_only(bank_id: str, require_memory_defense: bool) -> None:
    gateway = HindsightGateway(get_settings(), bank_id=bank_id)
    try:
        await _verify(gateway, require_memory_defense=require_memory_defense)
    finally:
        await gateway.close()


@app.command()
def apply(
    bank_id: str | None = typer.Option(None, help="Defaults to HINDSIGHT_BANK_ID."),
    webhook: bool = typer.Option(True, help="Register the retain.completed webhook."),
    require_memory_defense: bool = typer.Option(
        True, help="Fail if Hindsight did not enable Memory Defense."
    ),
) -> None:
    """Create the bank (if missing), converge its configuration and verify what Hindsight applied."""
    configure_logging(get_settings().log_level)
    asyncio.run(_apply(bank_id or get_settings().hindsight_bank_id, webhook, require_memory_defense))


@app.command()
def verify(
    bank_id: str | None = typer.Option(None, help="Defaults to HINDSIGHT_BANK_ID."),
    require_memory_defense: bool = typer.Option(True, help="Fail if Memory Defense is not active."),
) -> None:
    """Read back the bank's resolved configuration and exit non-zero if anything required is missing."""
    configure_logging(get_settings().log_level)
    asyncio.run(_verify_only(bank_id or get_settings().hindsight_bank_id, require_memory_defense))


@app.command()
def show(bank_id: str | None = typer.Option(None)) -> None:
    """Print the bank configuration as JSON."""
    config = build_config(bank_id or get_settings().hindsight_bank_id)
    typer.echo(json.dumps(asdict(config), indent=2))


def _expected_document_ids() -> set[str]:
    source = SeedSource(get_settings().data_dir)
    return {incident_document_id(incident["id"]) for incident in source.incidents} | {
        f"runbook:RB-{path.stem}" for path in source.runbook_paths()
    }


async def _audit(bank_id: str) -> BankAudit:
    settings = get_settings()
    gateway = HindsightGateway(settings, bank_id=bank_id)
    try:
        webhooks = await gateway.webhook_urls()
        return audit_bank(
            await gateway.list_documents(),
            expected_document_ids=_expected_document_ids(),
            directive_names=await gateway.directive_names(),
            webhook_registered=(
                settings.hindsight_webhook_url in webhooks
                if settings.hindsight_webhook_url
                else bool(webhooks)
            ),
            config=verification_report(await gateway.bank_config()),
        )
    finally:
        await gateway.close()


@app.command()
def audit(bank_id: str | None = typer.Option(None, help="Defaults to HINDSIGHT_BANK_ID.")) -> None:
    """Read-only. Exits non-zero if the bank differs from what PagerMind expects."""
    configure_logging(get_settings().log_level)
    report = asyncio.run(_audit(bank_id or get_settings().hindsight_bank_id))
    summary = {
        key: (len(value) if isinstance(value, list) else value) for key, value in report.to_dict().items()
    }
    summary["problems"] = report.problems
    summary["legacy_sample"] = report.legacy_documents[:5]
    summary["out_of_scope_sample"] = report.out_of_scope[:5]
    typer.echo(json.dumps(summary, indent=2))
    if report.problems:
        raise typer.Exit(code=1)


async def _prune(bank_id: str, apply_changes: bool) -> list[str]:
    report = await _audit(bank_id)
    if apply_changes and report.legacy_documents:
        gateway = HindsightGateway(get_settings(), bank_id=bank_id)
        try:
            for document_id in report.legacy_documents:
                await gateway.delete_document(document_id)
                log.info("legacy_document_deleted", document_id=document_id)
        finally:
            await gateway.close()
    return report.legacy_documents


@app.command("prune-legacy")
def prune_legacy(
    bank_id: str | None = typer.Option(None, help="Defaults to HINDSIGHT_BANK_ID."),
    apply_changes: bool = typer.Option(False, "--apply", help="Actually delete. Without it, only lists."),
) -> None:
    """Delete documents whose IDs PagerMind never writes (e.g. from the retired seed script)."""
    configure_logging(get_settings().log_level)
    legacy = asyncio.run(_prune(bank_id or get_settings().hindsight_bank_id, apply_changes))
    verb = "Deleted" if apply_changes else "Would delete (dry run, pass --apply)"
    typer.echo(
        f"{verb} {len(legacy)} legacy documents: {', '.join(legacy[:10])}{' ...' if len(legacy) > 10 else ''}"
    )


if __name__ == "__main__":
    app()
