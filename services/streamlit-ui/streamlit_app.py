"""Debug Agent Hub - Streamlit UI.

Pure frontend: calls the orchestrator's /health and /troubleshoot REST API
over its in-cluster Service. All search/ranking/RCA logic lives in the
orchestrator (app.py) - this app only renders it.
"""
import base64
import os
import time
from datetime import datetime, timezone

import httpx
import pandas as pd
import streamlit as st

ORCHESTRATOR_URL = os.environ.get(
    "ORCHESTRATOR_URL",
    "http://debug-agent-orchestrator.debug-agent-hub.svc.cluster.local:8080",
)
# Display-only default; the orchestrator's own HISTORY_RETENTION_DAYS env var
# is the source of truth for actual pruning - this just avoids a hardcoded
# "3" in the sidebar caption if that ever drifts. Overwritten below once
# /history responds with its real retention_days.
HISTORY_RETENTION_DAYS = 3

st.set_page_config(page_title="Debug Agent Hub", page_icon=None, layout="wide")

# --------------------------------------------------------------- brand icons --
# Real brand marks for Confluence / Red Hat / Slack / NotebookLM, embedded as
# self-contained base64 SVG data URIs (path data from the simple-icons set,
# recolored to this app's existing accent palette so they stay legible on the
# dark theme and match the colors already used elsewhere). No runtime CDN
# dependency, so this works uniformly everywhere a source appears: the header
# icon row and sidebar status rows (via a plain <img> tag through
# unsafe_allow_html), and the sidebar checkboxes + Team Config expander/tabs
# (via Markdown image syntax, which Streamlit widget labels support natively
# for Bold/Italic/Strikethrough/Code/Links/Images - see st.checkbox docs).
_BRAND_ICONS = {
    "CFL": {
        "color": "#73bcf7",
        "path": "M.87 18.257c-.248.382-.53.875-.763 1.245a.764.764 0 0 0 .255 1.04l4.965 3.054a.764.764 0 0 0 1.058-.26c.199-.332.454-.763.733-1.221 1.967-3.247 3.945-2.853 7.508-1.146l4.957 2.337a.764.764 0 0 0 1.028-.382l2.364-5.346a.764.764 0 0 0-.382-1 599.851 599.851 0 0 1-4.965-2.361C10.911 10.97 5.224 11.185.87 18.257zM23.131 5.743c.249-.405.531-.875.764-1.25a.764.764 0 0 0-.256-1.034L18.675.404a.764.764 0 0 0-1.058.26c-.195.335-.451.763-.734 1.225-1.966 3.246-3.945 2.85-7.508 1.146L4.437.694a.764.764 0 0 0-1.027.382L1.046 6.422a.764.764 0 0 0 .382 1c1.039.49 3.105 1.467 4.965 2.361 6.698 3.246 12.392 3.029 16.738-4.04z",
    },
    "KCS": {
        "color": "#ff8a82",
        "path": "M16.009 13.386c1.577 0 3.86-.326 3.86-2.202a1.765 1.765 0 0 0-.04-.431l-.94-4.08c-.216-.898-.406-1.305-1.982-2.093-1.223-.625-3.888-1.658-4.676-1.658-.733 0-.947.946-1.822.946-.842 0-1.467-.706-2.255-.706-.757 0-1.25.515-1.63 1.576 0 0-1.06 2.99-1.197 3.424a.81.81 0 0 0-.028.245c0 1.162 4.577 4.974 10.71 4.974m4.101-1.435c.218 1.032.218 1.14.218 1.277 0 1.765-1.984 2.745-4.593 2.745-5.895.004-11.06-3.451-11.06-5.734a2.326 2.326 0 0 1 .19-.925C2.746 9.415 0 9.794 0 12.217c0 3.969 9.405 8.861 16.851 8.861 5.71 0 7.149-2.582 7.149-4.62 0-1.605-1.387-3.425-3.887-4.512",
    },
    "SLK": {
        "color": "#f4c145",
        "path": "M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zM6.313 15.165a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313zM8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zM8.834 6.313a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312zM18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zM17.688 8.834a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312zM15.165 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zM15.165 17.688a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z",
    },
    "NBK": {
        "color": "#b2a3ff",
        "path": "M11.999 3.201C5.372 3.201 0 8.528 0 15.101V20.8h2.212v-.568c0-2.666 2.178-4.827 4.866-4.827 2.688 0 4.866 2.16 4.866 4.827v.568h2.212v-.568c0-3.877-3.17-7.019-7.078-7.019A7.075 7.075 0 0 0 2.992 14.5a7.355 7.355 0 0 1 6.568-4.016c4.057 0 7.347 3.264 7.347 7.287V20.8h2.212V17.77c0-5.235-4.28-9.481-9.56-9.481a9.563 9.563 0 0 0-6.217 2.28A9.795 9.795 0 0 1 12 5.393c5.406 0 9.788 4.346 9.788 9.707V20.8H24V15.1c-.001-6.573-5.373-11.9-12.001-11.9Z",
    },
}


def _icon_data_uri(source: str) -> str:
    meta = _BRAND_ICONS[source]
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'fill="{meta["color"]}"><path d="{meta["path"]}"/></svg>'
    )
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


def _icon_md(source: str, alt: str = "") -> str:
    """Markdown image snippet - usable inside st.checkbox/st.expander/st.tabs
    labels, which support a limited Markdown subset including images."""
    return f"![{alt}]({_icon_data_uri(source)})"


def _icon_img_html(source: str, size: int = 16, margin_right: str = "0.4rem") -> str:
    """Raw <img> tag - usable inside st.markdown(unsafe_allow_html=True)."""
    return (
        f'<img src="{_icon_data_uri(source)}" alt="{source}" '
        f'style="width:{size}px;height:{size}px;vertical-align:-3px;margin-right:{margin_right};" />'
    )


def inject_rhds_theme() -> None:
    """Approximate Red Hat Design System (ux.redhat.com) dark-mode styling on
    top of Streamlit's own theming (see config.toml for the base palette).
    Streamlit's [theme] block only exposes a handful of color knobs, so the
    rest (fonts, card/badge/button shapes) is layered on via CSS using Red
    Hat's own published web fonts and PatternFly's dark-theme tokens -- RHDS
    product UIs share PatternFly's token system, which is the closest public
    source of exact hex values for Red Hat's product (non-marketing) dark
    theme."""
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Red+Hat+Display:wght@500;600;700&family=Red+Hat+Text:wght@400;500;600&family=Red+Hat+Mono:wght@400;500&display=swap');

        html, body, [class*="css"], [data-testid="stAppViewContainer"] {
            font-family: 'Red Hat Text', -apple-system, BlinkMacSystemFont, sans-serif !important;
        }
        h1, h2, h3, h4, .rh-brandbar h1 {
            font-family: 'Red Hat Display', sans-serif !important;
            font-weight: 700 !important;
        }
        code, pre, kbd, [data-testid="stCodeBlock"] {
            font-family: 'Red Hat Mono', monospace !important;
        }

        /* App background - deep navy instead of flat grey */
        [data-testid="stAppViewContainer"], .stApp {
            background-color: #0b0d13 !important;
        }
        [data-testid="stHeader"] { background: transparent !important; }

        /* Brand bar (replaces st.title) */
        .rh-brandbar {
            display: flex; flex-direction: column; align-items: center;
            text-align: center;
            padding: 1.4rem 0 0.4rem; margin-bottom: 0.4rem;
        }
        /* Icon (left) + stacked title/subtitle (right). The title and
           subtitle must start AND end at the same horizontal point - not
           just share a center. Giving them independent widths (title
           hugging its own short text, subtitle hugging its own long text)
           can never satisfy that, since the two strings are different
           lengths no matter the font size. Instead, .rh-title-text sizes
           itself to fit-content (its widest natural child - normally the
           subtitle line), then BOTH children are forced to stretch to
           that exact same resolved width via width:100% + the parent's
           default flex align-items:stretch (no flex-start override here
           on purpose). That guarantees identical left/right box edges for
           the heading and the subtitle regardless of text length; they're
           then each centered within that shared box so the visible glyphs
           read as one matched block instead of two different-width lines. */
        .rh-title-row {
            display: flex; align-items: center; justify-content: center;
            gap: 1rem;
        }
        .rh-title-text {
            display: flex; flex-direction: column;
            width: fit-content; max-width: min(640px, 78vw);
        }
        .rh-brandbar h1 {
            margin: 0; font-size: 3.4rem; color: #f5f5f5;
            letter-spacing: 0.01em; line-height: 1.15;
            width: 100%; text-align: center;
        }
        .rh-title-icon-front {
            width: 4.6rem; height: 4.6rem; flex-shrink: 0;
        }
        .rh-subtitle {
            color: #ab9af2; margin: 0.4rem 0 0; font-size: 0.98rem;
            width: 100%; text-align: center; line-height: 1.5; font-weight: 500;
        }

        /* Real brand marks (Confluence/Red Hat/Slack/NotebookLM) are rendered
           as self-contained base64 SVG data URIs (see _icon_data_uri /
           _icon_md / _icon_img_html in the Python code below) - no runtime
           CDN dependency, so the same icon renders identically everywhere:
           header tiles, sidebar status rows, sidebar checkboxes, and the
           Team Config expander/tabs. This block only styles the big header
           tiles (.rh-icon-badge) that wrap those <img> tags. */
        .rh-icon-row { display: flex; justify-content: center; gap: 0.5rem; margin-bottom: 1.6rem; }
        .rh-icon-badge {
            display: inline-flex; align-items: center; justify-content: center;
            width: 34px; height: 34px; border-radius: 999px;
        }
        .rh-icon-badge img { width: 18px; height: 18px; vertical-align: middle !important; margin: 0 !important; }
        .rh-icon-badge.rh-icon-cfl { background: rgba(43,154,243,0.18);  border: 1px solid rgba(115,188,247,0.35); }
        .rh-icon-badge.rh-icon-kcs { background: rgba(238,0,0,0.16);     border: 1px solid rgba(255,138,130,0.35); }
        .rh-icon-badge.rh-icon-slk { background: rgba(236,178,46,0.18);  border: 1px solid rgba(244,193,69,0.35); }
        .rh-icon-badge.rh-icon-nbk { background: rgba(146,105,222,0.18); border: 1px solid rgba(178,163,255,0.35); }

        /* Checkbox/expander/tab labels render our Markdown-image icons via a
           plain <img src="data:..."> with no width/height attributes (the
           data URI SVGs only carry a viewBox). Without an explicit size,
           browsers fall back to an oversized default render box for SVG
           images with no intrinsic dimensions, which blows out the label's
           content width and forces Streamlit's wrap=False truncation to
           kick in early (full source names turning into "..."). Pin an
           explicit small size here so the icon never eats into the text's
           share of the available width. */
        [data-testid="stCheckbox"] img,
        [data-testid="stExpander"] summary img,
        [data-testid="stTabs"] img {
            width: 14px !important; height: 14px !important;
            vertical-align: -2px !important; margin-right: 0.15rem !important;
        }

        /* Sidebar */
        [data-testid="stSidebar"] {
            background-color: #12141c;
            border-right: 1px solid #262b38;
        }
        [data-testid="stSidebar"] h3 { color: #f0f0f0 !important; }

        /* Buttons -- rounded pill shape, cyan-to-blue gradient primary action */
        button[kind="primary"], button[kind="secondary"],
        [data-testid="stSidebar"] button {
            border-radius: 999px !important;
            font-weight: 600 !important;
            font-family: 'Red Hat Text', sans-serif !important;
        }
        button[kind="primary"] {
            background: linear-gradient(90deg, #00e5c0 0%, #0072bb 100%) !important;
            border: none !important;
            color: #ffffff !important;
            box-shadow: 0 4px 14px rgba(0, 229, 192, 0.35);
        }
        button[kind="primary"]:hover {
            background: linear-gradient(90deg, #1aedcb 0%, #0082d1 100%) !important;
            box-shadow: 0 6px 18px rgba(0, 229, 192, 0.5);
        }
        button[kind="secondary"], [data-testid="stSidebar"] button {
            background-color: #161a24 !important;
            border: 1px solid #2b2f3d !important;
            color: #f0f0f0 !important;
        }
        button[kind="secondary"]:hover, [data-testid="stSidebar"] button:hover {
            border-color: #a78bfa !important;
            color: #c9b8ff !important;
        }

        /* Query text box styling */
        [data-testid="stTextArea"] textarea {
            border-radius: 14px !important;
            background-color: #11131b !important;
            border: 1px solid #2a2e3d !important;
        }
        [data-testid="stTextArea"] textarea:focus {
            border-color: #6366f1 !important;
            box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.22) !important;
        }
        /* Reserve space in the bottom-right corner so typed text never runs
           under the overlaid reset button. */
        .st-key-query_box_wrap [data-testid="stTextArea"] textarea {
            padding-right: 72px !important;
            padding-bottom: 34px !important;
        }

        /* Streamlit's native "Press \u2318+Enter to apply" hint under the
           textarea - redundant now that there's an explicit Reset button,
           and the key hint doesn't apply to every user's keyboard anyway. */
        .st-key-query_box_wrap [data-testid="InputInstructions"] {
            display: none !important;
        }

        /* Query box reset button - a labeled "Reset" pill docked inside the
           textarea's own bottom-right corner (like a chat compose box), not
           beside the label. The button lives in the same
           st.container(key="query_box_wrap") as the text_area, so it's a
           normal sibling block in the DOM; pulling it out of flow with
           position:absolute (anchored to the wrapper, which is
           position:relative) is what lets it visually sit on top of the
           textarea instead of stacking below it. Targeted via the
           auto-generated st-key-<key> classes Streamlit adds to any widget/
           container with an explicit key= - no :has() marker trick needed. */
        .st-key-query_box_wrap {
            position: relative;
        }
        .st-key-query_box_wrap .st-key-reset_query_btn {
            position: absolute;
            right: 10px;
            bottom: 10px;
            z-index: 5;
        }
        .st-key-query_box_wrap .st-key-reset_query_btn button {
            height: 26px !important; min-height: 26px !important;
            padding: 0 0.7rem !important; border-radius: 999px !important;
            background: rgba(255,255,255,0.08) !important;
            border: 1px solid rgba(255,255,255,0.16) !important;
            color: #c3c7d1 !important;
            font-size: 0.75rem !important; line-height: 1 !important;
            box-shadow: 0 1px 4px rgba(0, 0, 0, 0.3) !important;
        }
        .st-key-query_box_wrap .st-key-reset_query_btn button:hover {
            background: rgba(255,255,255,0.18) !important;
            color: #ffffff !important;
        }

        /* Expander (Team Config) -> rounded card look */
        [data-testid="stExpander"] {
            border: 1px solid #262b38 !important;
            border-radius: 10px !important;
            background-color: #141722 !important;
        }

        /* Bordered containers -> rounded card look */
        div[data-testid="stVerticalBlockBorderWrapper"] {
            background-color: #141722 !important;
            border: 1px solid #262b38 !important;
            border-radius: 10px !important;
        }

        /* Empty-state ("Results will appear here") card */
        .rh-empty-state {
            border: 1px dashed #3a3f4e; border-radius: 14px;
            padding: 3rem 1rem; text-align: center; color: #868b98;
            margin-top: 0.5rem;
        }
        .rh-empty-state .rh-empty-icon { font-size: 2.4rem; display: block; margin-bottom: 0.7rem; }

        /* Credential "Configure" trigger (Confluence/KCS) - a plain gear
           icon, not a button: no circle, no border, no background, and no
           auto chevron (st.popover appends one by default to signal it
           expands, which reads as a dropdown). Clicking the bare icon still
           opens the same token-entry popover underneath. */
        [data-testid="stPopover"] button {
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
            padding: 0 !important;
            width: auto !important; height: auto !important; min-height: auto !important;
            border-radius: 0 !important;
        }
        [data-testid="stPopover"] button:hover {
            background: transparent !important;
            color: #c9b8ff !important;
        }
        [data-testid="stPopover"] button svg { display: none !important; }

        hr, [data-testid="stDivider"] { border-color: #262b38 !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )


inject_rhds_theme()

# Foreground colors reused from the former badge styling, kept here so the
# Ranked Matches table (below) stays visually consistent with the rest of
# the app's source/confidence color-coding.
_SOURCE_TEXT_COLOR = {"CFL": "#73bcf7", "NBK": "#b2a3ff", "KCS": "#ff8a82", "SLK": "#f4c145"}
_CONFIDENCE_TEXT_COLOR = {"high": "#4cb140", "medium": "#f0ab00", "low": "#b8bbbe"}


if "history" not in st.session_state:
    st.session_state.history = None
if "result" not in st.session_state:
    st.session_state.result = None
if "last_query" not in st.session_state:
    st.session_state.last_query = ""
if "result_mode" not in st.session_state:
    st.session_state.result_mode = "troubleshoot"
if "team_config" not in st.session_state:
    st.session_state.team_config = None
if "team_search_results" not in st.session_state:
    st.session_state.team_search_results = {"CFL": [], "NBK": [], "SLK": []}


_TEAM_SOURCE_META = {
    "CFL": {
        "label": "Confluence spaces",
        "list_key": "spaces",
        "placeholder": "keyword from a page in the space you want",
    },
    "NBK": {
        "label": "NotebookLM notebooks",
        "list_key": "notebooks",
        "placeholder": "notebook title keyword (leave blank to list all)",
    },
    "SLK": {
        "label": "Slack channels",
        "list_key": "channels",
        "placeholder": "keyword likely said in that channel",
    },
}


def render_team_config_tab(source: str) -> None:
    meta = _TEAM_SOURCE_META[source]
    if st.session_state.team_config is None:
        try:
            st.session_state.team_config = fetch_team_config()
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not load team config: {str(exc)[:300]}")
            return

    items = (st.session_state.team_config or {}).get(meta["list_key"]) or []
    st.caption(f"Configured {meta['label'].lower()} ({len(items)})")
    if not items:
        st.caption("None configured \u2014 this source falls back to a workspace-wide search.")
    else:
        for it in items:
            cols = st.columns([5, 1])
            cols[0].markdown(f"**{it.get('name', '')}**  \n`{it.get('key', '')}`")
            if cols[1].button("Remove", key=f"rm_{source}_{it.get('key')}", use_container_width=True):
                try:
                    result = remove_team_config_item(source, it.get("key", ""))
                    st.session_state.team_config[meta["list_key"]] = result.get("items", [])
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Failed to remove: {str(exc)[:300]}")

    st.divider()
    query = st.text_input(
        f"Search {meta['label'].lower()} to add",
        key=f"tc_search_input_{source}",
        placeholder=meta["placeholder"],
    )
    if st.button("Search", key=f"tc_search_btn_{source}"):
        if not query.strip() and source != "NBK":
            st.warning("Enter a search term first (no full directory listing is available for this source).")
        else:
            try:
                results = search_team_config(source, query.strip())
                st.session_state.team_search_results[source] = results
                if not results:
                    st.info("No matches found.")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Search failed: {str(exc)[:300]}")

    existing_keys = {it.get("key") for it in items}
    results = st.session_state.team_search_results.get(source) or []
    new_results = [r for r in results if r.get("key") not in existing_keys]
    if new_results:
        st.caption(f"Results ({len(new_results)})")
        for res in new_results:
            cols = st.columns([5, 1])
            cols[0].markdown(f"**{res.get('name', '')}**  \n`{res.get('key', '')}`")
            if cols[1].button("+ Add", key=f"add_{source}_{res.get('key')}", use_container_width=True):
                try:
                    result = add_team_config_item(
                        source, res["key"], res.get("name", res["key"]), res.get("category", "other")
                    )
                    st.session_state.team_config[meta["list_key"]] = result.get("items", [])
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Failed to add: {str(exc)[:300]}")


# ---------------------------------------------------------------- helpers --

def fetch_health() -> dict | None:
    try:
        r = httpx.get(f"{ORCHESTRATOR_URL}/health", timeout=10.0)
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreachable", "error": str(exc)[:300]}


def run_troubleshoot(query: str, sources: list[str]) -> dict:
    r = httpx.post(
        f"{ORCHESTRATOR_URL}/troubleshoot",
        json={"query": query, "sources": sources},
        timeout=180.0,
    )
    r.raise_for_status()
    return r.json()


def run_find_matches(query: str, sources: list[str]) -> dict:
    r = httpx.post(
        f"{ORCHESTRATOR_URL}/find-matches",
        json={"query": query, "sources": sources},
        timeout=60.0,
    )
    r.raise_for_status()
    return r.json()


def fetch_team_config() -> dict:
    r = httpx.get(f"{ORCHESTRATOR_URL}/team-config", timeout=15.0)
    r.raise_for_status()
    return r.json()


def search_team_config(source: str, query: str) -> list[dict]:
    r = httpx.get(
        f"{ORCHESTRATOR_URL}/team-config/search",
        params={"source": source, "query": query},
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json().get("results") or []


def add_team_config_item(source: str, key: str, name: str, category: str = "other") -> dict:
    r = httpx.post(
        f"{ORCHESTRATOR_URL}/team-config/items",
        json={"source": source, "key": key, "name": name, "category": category},
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()


def remove_team_config_item(source: str, key: str) -> dict:
    r = httpx.delete(
        f"{ORCHESTRATOR_URL}/team-config/items",
        params={"source": source, "key": key},
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()


def fetch_history() -> list[dict]:
    global HISTORY_RETENTION_DAYS
    r = httpx.get(f"{ORCHESTRATOR_URL}/history", timeout=15.0)
    r.raise_for_status()
    body = r.json()
    if body.get("retention_days"):
        HISTORY_RETENTION_DAYS = body["retention_days"]
    return body.get("items") or []


def _format_history_ts(ts: str) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%b %d, %H:%M")
    except (ValueError, TypeError):
        return ts[:16]


def post_history_item(query: str, sources: list[str], result: dict, mode: str) -> None:
    r = httpx.post(
        f"{ORCHESTRATOR_URL}/history",
        json={"query": query, "sources": sources, "result": result, "mode": mode},
        timeout=20.0,
    )
    r.raise_for_status()


def clear_history_backend() -> None:
    r = httpx.delete(f"{ORCHESTRATOR_URL}/history", timeout=15.0)
    r.raise_for_status()


def apply_credentials(source: str, token: str, username: str = "", url: str = "") -> dict:
    body: dict = {"token": token}
    if username:
        body["username"] = username
    if url:
        body["url"] = url
    r = httpx.post(f"{ORCHESTRATOR_URL}/credentials/{source}", json=body, timeout=20.0)
    r.raise_for_status()
    return r.json()


def apply_credentials_file(source: str, filename: str, file_bytes: bytes) -> dict:
    """Slack/NotebookLM - upload the ready-made Secret YAML produced by
    scripts/refresh-slack-creds.ps1 -Mode Export /
    scripts/refresh-nbk-creds.ps1 -SkipLogin -Mode Export. The orchestrator
    parses it and applies it via the same PATCH-secret-then-restart
    mechanism as the paste-a-token flow above."""
    r = httpx.post(
        f"{ORCHESTRATOR_URL}/credentials/{source}/file",
        files={"file": (filename, file_bytes, "application/x-yaml")},
        timeout=20.0,
    )
    r.raise_for_status()
    return r.json()


def status_dot(status: str | None) -> str:
    s = (status or "").lower()
    if s in ("ok", "configured"):
        color = "#4cb140"
    elif s in ("unreachable", "error"):
        color = "#c9190b"
    else:
        color = "#f0ab00"
    return f'<span style="color:{color}">\u25cf</span>'


if st.session_state.history is None:
    try:
        st.session_state.history = fetch_history()
    except Exception:  # noqa: BLE001
        st.session_state.history = []


if "cred_key_version" not in st.session_state:
    st.session_state.cred_key_version = {"CFL": 0, "KCS": 0, "SLK": 0, "NBK": 0}

# CFL (Confluence API token) and KCS (Red Hat SSO offline token) are static,
# user-visible credentials - paste-a-token Configure flow, posts to
# /credentials/{source}.
_TOKEN_CONNECTABLE = {"CFL": "Confluence", "KCS": "Red Hat KCS"}
# Slack (browser session cookies) and NotebookLM (interactive Google OAuth
# via `nlm login`) can't be safely typed into a text field - Configure flow
# is a file upload instead (the Secret YAML from
# scripts/refresh-slack-creds.ps1 -Mode Export /
# scripts/refresh-nbk-creds.ps1 -SkipLogin -Mode Export), posts to
# /credentials/{source}/file. Do not add other sources to either dict
# without also adding matching backend support.
_FILE_CONNECTABLE = {"SLK": "Slack", "NBK": "NotebookLM"}
_CONNECTABLE = {**_TOKEN_CONNECTABLE, **_FILE_CONNECTABLE}
_FILE_HINT = {
    "SLK": "scripts/refresh-slack-creds.ps1 -Mode Export",
    "NBK": "scripts/refresh-nbk-creds.ps1 -SkipLogin -Mode Export",
}


def render_status_row(label: str, status: str | None, source: str) -> None:
    """One sidebar status line, prefixed with the source's real brand mark.
    When `source` is connectable, a plain gear icon sits next to it with no
    button chrome (no circle, no dropdown chevron) - clicking it opens
    either a paste-a-token popover (CFL/KCS) or a file-upload popover
    (SLK/NBK)."""
    logo = _icon_img_html(source, size=16)
    if source not in _CONNECTABLE:
        st.markdown(f"{status_dot(status)} {logo}{label} \u2014 `{status or 'unknown'}`", unsafe_allow_html=True)
        return

    cols = st.columns([5, 1])
    cols[0].markdown(f"{status_dot(status)} {logo}{label} \u2014 `{status or 'unknown'}`", unsafe_allow_html=True)
    with cols[1].popover("\u2699\ufe0f", help=f"Configure {label}"):
        st.caption(f"Configure {label} connection.")
        version = st.session_state.cred_key_version[source]

        if source in _FILE_CONNECTABLE:
            st.caption(f"Generate the file first (on your own machine, no cluster access needed): `{_FILE_HINT[source]}`")
            uploaded = st.file_uploader(
                "Secret YAML file",
                type=["yaml", "yml"],
                key=f"cred_file_{source}_{version}",
            )
            if st.button("Upload & Apply", key=f"cred_submit_{source}_{version}", use_container_width=True):
                if uploaded is None:
                    st.error("Choose a file first.")
                else:
                    try:
                        apply_credentials_file(source, uploaded.name, uploaded.getvalue())
                        st.session_state.cred_key_version[source] += 1
                        st.success(f"{label} credentials applied \u2014 restarting MCP server\u2026")
                        time.sleep(4)
                        st.session_state.health = fetch_health()
                        st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Failed to apply: {str(exc)[:200]}")
            return

        if source == "CFL":
            cfl_url = st.text_input(
                "Confluence URL (optional)",
                key=f"cred_url_CFL_{version}",
                placeholder="Defaults to the pre-configured instance - only set this to override it",
            )
            cfl_user = st.text_input(
                "Email",
                key=f"cred_user_CFL_{version}",
            )
        token_value = st.text_input("API token", type="password", key=f"cred_token_{source}_{version}")
        if st.button("Connect", key=f"cred_submit_{source}_{version}", use_container_width=True):
            if not token_value.strip():
                st.error("Enter a token first.")
            else:
                try:
                    kwargs = {}
                    if source == "CFL":
                        kwargs["username"] = cfl_user.strip()
                        kwargs["url"] = cfl_url.strip()
                    apply_credentials(source, token_value.strip(), **kwargs)
                    st.session_state.cred_key_version[source] += 1
                    st.success(f"{label} credentials applied \u2014 restarting MCP server\u2026")
                    time.sleep(4)
                    st.session_state.health = fetch_health()
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Failed to apply: {str(exc)[:200]}")


# ------------------------------------------------------------------ sidebar --

with st.sidebar:
    st.subheader("\U0001f5c4\ufe0f Source status")
    if st.button("\U0001f504 Check status", use_container_width=True):
        st.session_state.health = fetch_health()
    if "health" not in st.session_state:
        st.session_state.health = fetch_health()
    health = st.session_state.health or {}
    render_status_row("Confluence", health.get("confluence_status"), "CFL")
    render_status_row("Red Hat KCS", health.get("kcs_status"), "KCS")
    render_status_row("Slack", health.get("slack_status"), "SLK")
    render_status_row("NotebookLM", health.get("notebooklm_auth_status"), "NBK")
    if health.get("status") == "unreachable":
        st.error(f"Orchestrator unreachable: {health.get('error', '')}")

    st.divider()
    st.subheader("\U0001f3af Sources to search")
    use_cfl = st.checkbox(f"{_icon_md('CFL', 'Confluence')} Confluence", value=True, key="use_cfl", wrap=False)
    use_kcs = st.checkbox(f"{_icon_md('KCS', 'Red Hat KCS')} Red Hat KCS", value=True, key="use_kcs", wrap=False)
    use_slk = st.checkbox(f"{_icon_md('SLK', 'Slack')} Slack", value=True, key="use_slk", wrap=False)
    use_nbk = st.checkbox(f"{_icon_md('NBK', 'NotebookLM')} NotebookLM", value=True, key="use_nbk", wrap=False)

    st.divider()
    hist_hdr = st.columns([3, 1.2])
    hist_hdr[0].subheader("\U0001f552 Query history")
    hist_hdr[1].caption(f"kept {int(HISTORY_RETENTION_DAYS)}d")
    if st.session_state.history:
        if st.button("\U0001f5d1\ufe0f Clear history", key="clear_history_btn", use_container_width=True):
            try:
                clear_history_backend()
            except Exception as exc:  # noqa: BLE001
                st.error(f"Failed to clear on server: {str(exc)[:200]}")
            st.session_state.history = []
            st.rerun()
    if not st.session_state.history:
        st.caption(f"No queries in the last {int(HISTORY_RETENTION_DAYS)} days.")
    else:
        for i, item in enumerate(st.session_state.history):
            mode_tag = "Find Similar" if item.get("mode") == "find-matches" else "Troubleshoot"
            label = item["query"] if len(item["query"]) <= 34 else item["query"][:31] + "..."
            when = _format_history_ts(item.get("ts", ""))
            if st.button(f"{when} \u00b7 {mode_tag}: {label}", key=f"hist_{i}", use_container_width=True, help=item["query"]):
                st.session_state.result = item["result"]
                st.session_state.last_query = item["query"]
                st.session_state.result_mode = item.get("mode", "troubleshoot")
                st.rerun()


# ------------------------------------------------------------------- main --

st.markdown(
    f"""
    <div class="rh-brandbar">
        <div class="rh-title-row">
            <img src="app/static/retargeting.gif" alt="" class="rh-title-icon-front" />
            <div class="rh-title-text">
                <h1>Debug Agent Hub</h1>
                <div class="rh-subtitle">AI-powered root-cause analysis across your knowledge bases &amp; chat history.</div>
            </div>
        </div>
        <div class="rh-icon-row">
            <span class="rh-icon-badge rh-icon-cfl" title="Confluence">{_icon_img_html('CFL', size=18, margin_right='0')}</span>
            <span class="rh-icon-badge rh-icon-kcs" title="Red Hat KCS">{_icon_img_html('KCS', size=18, margin_right='0')}</span>
            <span class="rh-icon-badge rh-icon-slk" title="Slack">{_icon_img_html('SLK', size=18, margin_right='0')}</span>
            <span class="rh-icon-badge rh-icon-nbk" title="NotebookLM">{_icon_img_html('NBK', size=18, margin_right='0')}</span>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.expander(
    "Team Config \u2014 Confluence spaces / Slack channels / NotebookLM notebooks   "
    f"{_icon_md('CFL')} {_icon_md('SLK')} {_icon_md('NBK')}",
    icon="\u2699\ufe0f",
    expanded=False,
):
    st.caption(
        "Search a source and add results to your team's scope, or remove entries you no longer need."
    )
    tc_tabs = st.tabs(
        [f"{_icon_md('CFL')} Confluence", f"{_icon_md('SLK')} Slack", f"{_icon_md('NBK')} NotebookLM"]
    )
    for tc_tab, tc_source in zip(tc_tabs, ("CFL", "SLK", "NBK")):
        with tc_tab:
            render_team_config_tab(tc_source)


def _clear_query() -> None:
    """Reset clears the query box AND the displayed results (Ranked
    Matches, RCA, Prompt Chain) - previously this only cleared
    query_input, leaving the last run's result rendered below an empty
    box, which looked like the two were out of sync / the old answer
    belonged to whatever got typed next."""
    st.session_state.query_input = ""
    st.session_state.result = None
    st.session_state.result_mode = "troubleshoot"
    st.session_state.last_query = ""


with st.container(key="query_box_wrap"):
    query = st.text_area(
        "Troubleshooting query",
        key="query_input",
        placeholder="e.g. pod CrashLoopBackOff after OOMKill on worker-1\u2026",
        height=120,
    )
    st.button(
        "Reset",
        key="reset_query_btn",
        help="Clear the query box and the displayed results",
        on_click=_clear_query,
    )

col_btn, col_btn2, col_status = st.columns([1.5, 1.5, 2.5])
with col_btn:
    run_clicked = st.button("🚀 Troubleshoot", type="primary", use_container_width=True)
with col_btn2:
    find_clicked = st.button(
        "\U0001f50d Find Similar",
        type="secondary",
        use_container_width=True,
        help="Fast search-only pass: classify + fan-out + rank, no RCA synthesis.",
    )
with col_status:
    status_placeholder = st.empty()

if run_clicked or find_clicked:
    text = query.strip()
    if not text:
        st.error("Enter a query first.")
    else:
        selected = [s for s, on in (("CFL", use_cfl), ("KCS", use_kcs), ("SLK", use_slk), ("NBK", use_nbk)) if on]
        if not selected:
            st.error("Select at least one source.")
        else:
            mode = "find-matches" if find_clicked else "troubleshoot"
            if mode == "find-matches":
                status_placeholder.info(f"Searching {', '.join(selected)} and ranking matches\u2026")
            else:
                status_placeholder.info(f"Searching {', '.join(selected)}, classifying, ranking, synthesizing RCA\u2026")
            t0 = time.time()
            try:
                result = run_find_matches(text, selected) if mode == "find-matches" else run_troubleshoot(text, selected)
                elapsed = round(time.time() - t0, 1)
                status_placeholder.success(f"Done in {elapsed}s")
                st.session_state.result = result
                st.session_state.last_query = text
                st.session_state.result_mode = mode
                try:
                    post_history_item(text, selected, result, mode)
                except Exception:  # noqa: BLE001
                    pass
                st.session_state.history.insert(0, {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "query": text,
                    "sources": selected,
                    "result": result,
                    "mode": mode,
                })
                st.session_state.history = st.session_state.history[:50]
            except Exception as exc:  # noqa: BLE001
                status_placeholder.error(f"Failed: {str(exc)[:400]}")
                st.session_state.result = None

result = st.session_state.result
result_mode = st.session_state.result_mode

if result:
    if st.session_state.last_query:
        mode_label = "Find Similar (search-only)" if result_mode == "find-matches" else "Troubleshoot (full RCA)"
        st.caption(f"Showing **{mode_label}** results for: **{st.session_state.last_query}**")

    classification = result.get("classification") or {}
    if classification.get("persona") or classification.get("template"):
        with st.container(border=True):
            cols = st.columns([1, 1, 3])
            cols[0].markdown(f"**Persona**  \n{classification.get('persona', '')}")
            cols[1].markdown(f"**Template**  \n{classification.get('template', '')}")
            cols[2].markdown(f"**Rationale**  \n{classification.get('rationale', '')}")

    warnings = result.get("warnings") or []
    if warnings:
        st.warning("\n\n".join(warnings))

    st.subheader("Ranked Matches")
    ranked = result.get("rankedMatches") or result.get("matches") or []
    if not ranked:
        st.caption("No ranked matches from the selected sources.")
    else:
        df = pd.DataFrame([
            {
                "Rank": m.get("rank", i + 1),
                "Source": m.get("source", ""),
                "Score": m.get("score", 0),
                "Confidence": (m.get("confidence") or "").capitalize(),
                "Title": m.get("title", ""),
                "Link": m.get("sourceUrl") or m.get("url") or "",
                "Probable Answer": m.get("probableAnswer", ""),
                "Why": m.get("why", ""),
            }
            for i, m in enumerate(ranked)
        ])

        def _style_source(val):
            color = _SOURCE_TEXT_COLOR.get(val, "")
            return f"color: {color}; font-weight: 600" if color else ""

        def _style_confidence(val):
            color = _CONFIDENCE_TEXT_COLOR.get((val or "").lower(), "")
            return f"color: {color}; font-weight: 600" if color else ""

        try:
            table_data = df.style.applymap(_style_source, subset=["Source"]) \
                                  .applymap(_style_confidence, subset=["Confidence"])
        except Exception:  # noqa: BLE001 - styling is cosmetic only, never let it block the table
            table_data = df

        st.dataframe(
            table_data,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Rank": st.column_config.NumberColumn("#", width="small"),
                "Score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%d"),
                "Confidence": st.column_config.TextColumn("Confidence", width="small"),
                "Title": st.column_config.TextColumn("Title", width="medium"),
                "Link": st.column_config.LinkColumn("Open", display_text="\u2197", width="small"),
                "Probable Answer": st.column_config.TextColumn("Probable Answer", width="large"),
                "Why": st.column_config.TextColumn("Why", width="medium"),
            },
        )

    if result_mode == "find-matches":
        st.caption(
            "Find Similar is a search-only pass \u2014 classify + fan-out + rank, "
            "no RCA synthesis. Click **Troubleshoot** on the same query for a full root-cause writeup."
        )
    else:
        prompt_chain = result.get("promptChain") or []
        if prompt_chain:
            st.subheader("Prompt Chain")
            for step in prompt_chain:
                with st.container(border=True):
                    st.markdown(f"**Step {step.get('step', '')}: {step.get('template', '')}**")
                    st.markdown(step.get("summary", ""))

        st.subheader("RCA")
        if result.get("noResults"):
            st.caption("No documentation found for this query across selected sources.")
        else:
            rca = result.get("rca") or {}
            if isinstance(rca, str):
                st.markdown(rca or "Results will appear here.")
            else:
                st.markdown("**Summary**")
                st.write(rca.get("summary", ""))

                st.markdown("**Root Cause**")
                st.write(rca.get("rootCause", ""))

                steps = rca.get("steps") or []
                if steps:
                    st.markdown("**Resolution Steps**")
                    for i, s in enumerate(steps, start=1):
                        # Each step is grounded in one or more sources
                        # (CFL/NBK/KCS/SLK) per run_prompt_chain()'s RCA
                        # system prompt, but that attribution used to be
                        # silently dropped here - the step text rendered
                        # with no indication of which source backed it,
                        # even when it came straight from a KCS solution.
                        step_sources = s.get("sources") or []
                        tags = " ".join(
                            f":{('blue' if src == 'CFL' else 'violet' if src == 'NBK' else 'red' if src == 'KCS' else 'orange')}[{src}]"
                            for src in step_sources
                        )
                        prefix = f"{tags} " if tags else ""
                        st.markdown(f"{i}. {prefix}**{s.get('action', '')}** \u2014 {s.get('detail', '')}")

                st.markdown("**Verification**")
                st.write(rca.get("verification", ""))
else:
    st.markdown(
        """
        <div class="rh-empty-state">
            <span class="rh-empty-icon">\U0001f50e</span>
            Results will appear here.
        </div>
        """,
        unsafe_allow_html=True,
    )