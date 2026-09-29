"""PagerMind - an on-call assistant that remembers every past incident (powered by Hindsight).

Two answers for every alert, side by side:
  Without Hindsight - a normal LLM API call (Groq): just the alert and its logs
  With Hindsight    - Hindsight reflect: the same alert and logs + the team's memory of 61 past incidents

Run:  streamlit run app.py
"""

import os

import streamlit as st

# Streamlit Community Cloud: copy secrets into env before the config is read
try:
    for _k, _v in st.secrets.items():
        os.environ.setdefault(_k, str(_v))
except Exception:
    pass

import pandas as pd  # noqa: E402

from pagermind import data  # noqa: E402
from pagermind.agent import Agent  # noqa: E402
from pagermind.config import get_config  # noqa: E402
from pagermind.memory import PagerMindMemory  # noqa: E402

st.set_page_config(page_title="PagerMind", page_icon="🧠", layout="wide")
st.markdown("""
<style>
.chip {display:inline-block;padding:1px 8px;border-radius:8px;background:#e0e7ff;color:#3730a3;font-size:0.75rem;margin-right:4px}
.alert {border-left:6px solid #dc2626;padding:10px 16px;background:rgba(220,38,38,0.06);border-radius:6px}
</style>""", unsafe_allow_html=True)


@st.cache_resource
def services():
    cfg = get_config()
    return PagerMindMemory(cfg), Agent(cfg)


mem, agent = services()


@st.cache_data(ttl=300, show_spinner=False)
def memory_status() -> tuple[bool, int]:
    try:
        return True, len([i for i in mem.stored_incident_ids() if not i.endswith("-steps")])
    except Exception:
        return False, 0


ss = st.session_state
ss.setdefault("incident", None)

# --------------------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("🧠 PagerMind")
    st.caption("On-call assistant for **Kartly** that remembers every past incident.")
    up, count = memory_status()
    if up:
        st.success(f"Hindsight bank `{mem.bank}` - {count} incidents remembered")
    else:
        st.error("Hindsight is unreachable - only the 'without Hindsight' answer is available")
    st.divider()
    st.markdown("""**Two answers for every alert**
- **Without Hindsight** - a normal LLM call: just the alert and its logs
- **With Hindsight** - the same alert and logs, plus `reflect` over the team's memory of past incidents

**The memory learns**
- 👍 / 👎 on a step -> `retain`
- Resolved incident -> `retain` -> recalled next time""")

tab_oncall, tab_history, tab_how = st.tabs(["🚨 On-call", "📚 Past incidents", "🔧 How it uses Hindsight"])


# --------------------------------------------------------------------------- helpers
def answer_without_hindsight(inc: dict) -> dict:
    """A plain LLM call - only the alert and its logs."""
    return agent.suggest_without_memory(alert=inc["alert"], log_lines=inc["logs"])


def answer_with_hindsight(inc: dict) -> dict:
    """The same alert and logs, answered by Hindsight reflect over the team's memory."""
    try:
        return mem.reflect_suggestion(inc["alert"], inc["logs"], "Current logs:\n" + "\n".join(inc["logs"]))
    except Exception as e:
        return {"summary": f"Hindsight is unavailable right now ({e.__class__.__name__}).",
                "likely_cause": "unknown", "matched_incidents": [], "avoid": [], "memory": [], "facts_used": 0,
                "recommended_steps": [{"order": 1, "action": "Try again in a minute", "why": ""}],
                "model": "Hindsight reflect", "seconds": 0, "text": ""}


def chips(ids: list[str]) -> str:
    return "".join(f'<span class="chip">{i}</span>' for i in ids)


def render_card(card: dict, inc: dict, key: str, feedback: bool) -> None:
    st.caption(f"{card.get('model')} · {card.get('seconds')}s")
    st.markdown(f"**{card['summary']}**")
    st.markdown(f"**Likely cause:** {card.get('likely_cause', '')}")
    if card.get("matched_incidents"):
        st.markdown("**Seen before:** " + chips([m["incident_id"] for m in card["matched_incidents"]]), unsafe_allow_html=True)
    elif key == "with":
        st.markdown("**Seen before:** no matching past incident - this looks new")
    if card.get("avoid"):
        st.markdown("**❌ Don't do this**")
        for a in card["avoid"]:
            st.markdown(f"- {a['action']} - _{a['why']}_ {chips(a.get('evidence_incident_ids', []))}", unsafe_allow_html=True)
    st.markdown("**✅ Do this**")
    for s in card.get("recommended_steps", []):
        st.markdown(f"{s.get('order', '')}. **{s['action']}** - {s.get('why', '')} {chips(s.get('evidence_incident_ids', []))}",
                    unsafe_allow_html=True)
        if s.get("command"):
            st.code(s["command"], language="bash")
        if feedback and up:
            c1, c2, _ = st.columns([1, 1, 3])
            for col, label, outcome in ((c1, "👍 Worked", "worked"), (c2, "👎 Didn't work", "did_not_work")):
                if col.button(label, key=f"{key}-{s.get('order')}-{outcome}"):
                    mem.retain_step(incident_id=inc["id"], service=inc["service"], actor="On-call engineer",
                                    action=s["action"], outcome=outcome)
                    inc["steps"].append({"action": s["action"], "outcome": outcome})
                    st.toast(f"Saved to Hindsight: '{s['action'][:40]}' -> {outcome}", icon="🧠")
    if card.get("escalate_to"):
        st.caption(f"Escalate to: {card['escalate_to']}")


def render_memory(card: dict) -> None:
    st.markdown(f"#### 🧠 Memory used by Hindsight ({card.get('facts_used', 0)} facts)")
    if not card.get("memory"):
        st.info("No past incidents were used.")
    for m in card.get("memory", []):
        matched = m.incident_id in {x["incident_id"] for x in card.get("matched_incidents", [])}
        with st.expander(f"{'✅ ' if matched else ''}{m.incident_id} · {m.service or ''} · {m.date or ''}"):
            if m.title:
                st.caption(m.title)
            for f in m.facts[:6]:
                st.markdown(f"- {f}")
    if card.get("text"):
        with st.expander("Hindsight's full reasoning"):
            st.markdown(card["text"])


def next_incident_id() -> str:
    nums = [int(h["id"].split("-")[1]) for h in data.history()]
    try:
        nums += [int(i.split("-")[1]) for i in mem.stored_incident_ids() if i.startswith("INC-") and i.count("-") == 1]
    except Exception:
        pass
    return f"INC-{max(nums) + 1}"


# --------------------------------------------------------------------------- on-call tab
with tab_oncall:
    scenarios = data.scenarios()
    labels = {f"{s['alert']['title']}  ({s['service']})  -  {s['name']}": s for s in scenarios}
    c1, c2 = st.columns([4, 1])
    choice = c1.selectbox("Simulate an alert", list(labels), label_visibility="collapsed")
    if c2.button("🚨 Fire alert", type="primary", width="stretch"):
        sc = labels[choice]
        ss.incident = {"id": next_incident_id(), "scenario": sc["id"], "service": sc["service"], "alert": sc["alert"],
                       "severity": sc["severity"], "logs": sc["log_excerpt"], "without": None, "with": None,
                       "steps": [], "postmortem": None}

    inc = ss.incident
    if not inc:
        st.info("Pick an alert and press **Fire alert**, then get both answers and compare them.")
    else:
        a = inc["alert"]
        st.markdown(f'<div class="alert"><b>{inc["id"]} · {inc["severity"]} · {inc["service"]}</b><br>{a["title"]}<br>'
                    f'<small>{a["value"]} · fired {a["fired_at"][:16].replace("T", " ")} IST</small></div>', unsafe_allow_html=True)
        with st.expander("Current logs"):
            st.code("\n".join(inc["logs"]), language="log")

        b1, b2 = st.columns(2)
        if b1.button("⚪ Without Hindsight (normal LLM call)", width="stretch"):
            with st.spinner("Calling the LLM with the alert and logs..."):
                inc["without"] = answer_without_hindsight(inc)
        if b2.button("🧠 With Hindsight (reflect)", type="primary", width="stretch", disabled=not up):
            with st.spinner("Hindsight is reflecting over the team's memory of past incidents (10-20s)..."):
                inc["with"] = answer_with_hindsight(inc)

        left, right = st.columns(2)
        with left:
            st.subheader("Without Hindsight")
            if inc["without"]:
                render_card(inc["without"], inc, "without", feedback=False)
            else:
                st.caption("Not run yet.")
        with right:
            st.subheader("With Hindsight 🧠")
            if inc["with"]:
                render_card(inc["with"], inc, "with", feedback=True)
            else:
                st.caption("Not run yet.")
        if inc["with"]:
            render_memory(inc["with"])

        if inc["with"]:
            st.divider()
            st.markdown("#### ✅ Resolve and teach the team memory")
            if inc["steps"]:
                st.caption("Saved to Hindsight: " + "; ".join(f"{s['action'][:40]} -> {s['outcome']}" for s in inc["steps"]))
            if st.button("Draft postmortem"):
                with st.spinner("Drafting..."):
                    inc["postmortem"] = agent.draft_postmortem(alert=a, card=inc["with"], steps=inc["steps"])
            if inc["postmortem"] is not None:
                text = st.text_area("Postmortem (edit before saving)", inc["postmortem"], height=180)
                if st.button("💾 Save to Hindsight", type="primary", disabled=not up):
                    mem.retain_resolution(incident_id=inc["id"], service=inc["service"], title=a["title"], summary=text)
                    memory_status.clear()
                    st.success(f"{inc['id']} saved to Hindsight. Fire the same alert again - it will be recalled.")
                    ss.incident = None

# --------------------------------------------------------------------------- history tab
with tab_history:
    hist = data.history()
    st.markdown(f"**{len(hist)} past incidents** (Jan-Sep 2026) were loaded into Hindsight with `retain_batch`. "
                "The app never reads this table when answering - the 'with Hindsight' answer only knows what Hindsight remembers.")
    df = pd.DataFrame([{"id": h["id"], "date": h["date"][:10], "service": h["service"], "severity": h["severity"],
                        "title": h["title"], "minutes to fix": h["mttr_minutes"], "resolved by": h["resolved_by"]}
                       for h in reversed(hist)])
    st.dataframe(df, width="stretch", hide_index=True)
    pick = st.selectbox("Read a postmortem", [h["id"] for h in reversed(hist)])
    st.markdown(next(h for h in hist if h["id"] == pick)["postmortem_summary"])

# --------------------------------------------------------------------------- how tab
with tab_how:
    st.markdown("""
### How PagerMind uses Hindsight

| Step | Hindsight call | What happens |
|---|---|---|
| Setup | `create_bank(mission, retain_mission, observations_mission, reflect_mission, disposition)` | One bank = the on-call team's memory |
| Load history | `retain_batch(items)` | 61 past incidents, one document each, tagged `service:` / `severity:` |
| Alert fires | `reflect(query, context=logs, response_schema, include_facts=True)` | Hindsight reasons over the whole bank and returns the suggestion card as JSON, plus the facts it used |
| Guardrails | (our code) | Only incidents Hindsight actually used can be cited; no "seen before" without a strong match; never "avoid" what is recommended |
| Engineer feedback | `retain(update_mode="append")` | 👍 / 👎 on each step becomes a memory - failures too |
| Resolved | `retain(document_id=INC-xxxx)` | The new incident is remembered for the next alert |
| Quality check | `recall()` ×2 (scripts/check_recall.py) | Error-signature + symptom search: 8/8 demo alerts find the right past incidents |

Both answers get exactly the same input - the alert and its logs.
**Without Hindsight** a plain LLM call gives generic advice from the logs: *"check DB health, look for slow queries, scale the database"*.
For the notification-service memory leak it even says *avoid restarting* - yet a restart fixed that exact leak 4 times.

**With Hindsight** reflect says *"same as INC-2057 and 3 others - the recon job is holding the connections: don't restart, suspend the recon job"*,
and for the memory leak: *"do a rolling restart now - it worked 4 times"*.
""")
