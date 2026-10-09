import { expect, test } from "@playwright/test";

const LOG = [
  "2026-09-29 02:40:36.698  WARN 1 --- [http-nio-8080-exec-132] com.zaxxer.hikari.pool.HikariPool : " +
    "HikariPool-1 - Connection is not available, request timed out after 30000ms.",
  "2026-09-29 02:40:37.120 ERROR 1 --- [http-nio-8080-exec-17] c.k.payments.api.ChargeController : " +
    "Charge failed: could not obtain JDBC connection",
].join("\n");

test("upload → diagnose → feedback → resolve", async ({ page }) => {
  const operatorToken = process.env.E2E_OPERATOR_TOKEN;
  test.skip(!operatorToken, "E2E_OPERATOR_TOKEN is required to resolve incidents");

  await page.goto("/");
  const engineer = page.getByLabel("On-call engineer");
  await expect(engineer.locator("option")).not.toHaveCount(1);
  await engineer.selectOption({ index: 1 });

  // 1. Upload a diagnostic log.
  await page.locator('input[type="file"]').setInputFiles({
    name: "payments.log",
    mimeType: "text/plain",
    buffer: Buffer.from(LOG),
  });
  await expect(page.locator(".attachments .chip")).toHaveCount(1);

  // 2-3. Memory-grounded recommendation next to the baseline.
  await page.getByPlaceholder(/Describe the incident/).fill("payments-api p99 latency 10s and 5xx at 13%.");
  await page.getByRole("button", { name: "Diagnose" }).click();
  await expect(page.getByRole("heading", { name: "Without memory" })).toBeVisible({ timeout: 90_000 });
  const answer = page.locator(".answer.hindsight");
  await expect(answer.locator(".badge").first()).toBeVisible({ timeout: 90_000 });
  await expect(answer.getByText("Recommended actions")).toBeVisible();

  // 4. Provisional feedback on the first step.
  const step = answer.locator(".steps > li").first();
  await step.getByPlaceholder(/What happened/).fill("Suspending recon-job restored the pool within four minutes.");
  await step.getByRole("button", { name: /Worked/ }).click();
  await expect(step.getByText(/Provisional/)).toBeVisible();

  // 5-6. Resolve with the operator key, verify the feedback, watch the memory job.
  const resolve = page.locator("form.resolve");
  await resolve.getByLabel("Operator key").fill(operatorToken!);
  await resolve.getByRole("button", { name: "Load provisional feedback" }).click();
  await resolve.locator(".review-row select").first().selectOption("verify");
  await resolve.getByLabel("Confirmed root cause").fill("recon-job held Postgres connections during peak traffic.");
  await resolve.getByLabel("What fixed it").fill("Suspended recon-job and rescheduled it off-peak.");
  await resolve.getByRole("button", { name: "Resolve and remember" }).click();
  await expect(page.getByText(/resolved/)).toBeVisible();
  const loop = page.locator(".learning-loop");
  await expect(loop.locator(".stepper .done", { hasText: "Retained" })).toBeVisible({ timeout: 120_000 });

  // The same alert, as a new incident, must now cite the incident that was just resolved.
  await loop.getByRole("button", { name: /Replay alert A/ }).click();
  await expect(loop.getByText(/Learned: .* is now evidence/)).toBeVisible({ timeout: 120_000 });
});
