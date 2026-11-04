"""指纹单测：成本控制与报告正确性的根基（TDD 9.1）。"""

from pathlib import Path

from competitor_intel.core.fingerprint import compute_fingerprint

FIXTURE = Path(__file__).parent / "fixtures" / "sample_pricing.html"


def test_same_content_same_hash():
    html = FIXTURE.read_text(encoding="utf-8")
    assert compute_fingerprint(html, "main") == compute_fingerprint(html, "main")


def test_whitespace_is_normalized():
    html_a = "<main><p>Pro ¥99/月</p></main>"
    html_b = "<main><p>Pro   ¥99/月\n</p></main>"
    assert compute_fingerprint(html_a, "main") == compute_fingerprint(html_b, "main")


def test_noise_outside_container_ignored():
    """容器外的噪声（时间戳、推荐位）不触发伪变更。"""
    base = "<main><p>Pro ¥99</p></main>"
    noisy = base + "<footer>最后更新：2026-11-01</footer>"
    assert compute_fingerprint(base, "main") == compute_fingerprint(noisy, "main")


def test_content_change_changes_hash():
    a = "<main><p>Pro ¥99</p></main>"
    b = "<main><p>Pro ¥129</p></main>"
    assert compute_fingerprint(a, "main") != compute_fingerprint(b, "main")


def test_missing_selector_yields_stable_hash():
    assert compute_fingerprint("<div></div>", "main") == compute_fingerprint("", "main")
