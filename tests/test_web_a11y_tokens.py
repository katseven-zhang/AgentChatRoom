"""#330: shipped CSS tokens and dialog accessibility."""

from __future__ import annotations

import re
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "agentchatroom" / "web"


def test_dialogs_name_their_headings_and_transport_choice_is_a_group():
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    dialogs = re.findall(r"<dialog\b[^>]*>", markup)
    assert len(dialogs) >= 13
    for dialog in dialogs:
        assert "aria-labelledby=" in dialog or "aria-label=" in dialog
        labelledby = re.search(r'aria-labelledby="([^"]+)"', dialog)
        assert labelledby, dialog
        assert f'id="{labelledby.group(1)}"' in markup
    assert 'role="group" aria-label="连接方式（仅 HTTP）"' in markup
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")
    hover = stylesheet[
        stylesheet.index(".agent-item:hover {") : stylesheet.index(".agent-item.is-disconnected")
    ]
    assert "translateY" not in hover


def test_z_index_line_height_and_dead_blocks_follow_the_tokens():
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")
    light = stylesheet[: stylesheet.index(':root[data-theme="dark"]')]
    assert "--danger-soft:" in light
    assert "z-index: var(--z-sticky)" in stylesheet
    assert "z-index: var(--z-dialog)" in stylesheet
    assert "z-index: var(--z-toast)" in stylesheet
    assert not re.search(r"z-index:\s*\d", stylesheet)
    assert ".integration-transport-tabs" in stylesheet
    assert ".integration-tabs" not in stylesheet
    assert ".check-row" in stylesheet
    for dead in (".tab-status", ".merged-activity", ".dependency-options", ".form-grid"):
        assert dead not in stylesheet
    assert "line-height: 1.3" not in stylesheet
    assert "line-height: 1.35" not in stylesheet
    assert "line-height: 1.4" not in stylesheet
    narrow = stylesheet[stylesheet.index("@media (max-width: 760px)") :]
    assert "height: auto;" in narrow[narrow.index(".audit-list .management-item") :]
    readme = (WEB_DIR.parents[2] / "README.md").read_text(encoding="utf-8")
    assert "--lh-tight" in readme
    assert "--lh-body" in readme
