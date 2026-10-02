"""Web surface on Playwright (Chromium).

Legacy-web specifics handled here:

* **Frames.** Every target carries a frame path (``[{name: main}]``). Frames are looked up fresh
  on each call, so a frame that reloads between steps is not a problem.
* **Locators without test ids.** Role + name uses Playwright's accessibility engine; label,
  table-cell, text and attribute strategies run in ``dom.js`` because legacy markup puts labels
  in the neighbouring ``<td>`` where the accessibility tree cannot see them.
* **Uniqueness.** A strategy wins only on exactly one visible match. ``near_text`` narrows
  duplicates (two "Search" buttons) to the one closest to that text.
* **Navigation guard.** Every document request (top level and frames) passes ``url_guard``
  before it leaves the browser. A link to ``/admin/transfer`` is aborted at the network layer
  even if something manages to click it. This is defence in depth under the policy guard.
* **Native dialogs** (alert/confirm) are recorded, then accepted (alert) or dismissed
  (confirm/prompt). Dismissing is the safe default; the record lets detectors escalate.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Dialog,
    ElementHandle,
    Frame,
    Page,
    Request,
    Route,
    sync_playwright,
)
from playwright.sync_api import Error as PlaywrightError

from cua.core.conditions import PageState
from cua.core.targets import (
    AttributeStrategy,
    CssStrategy,
    FrameRef,
    LabelStrategy,
    RoleStrategy,
    Strategy,
    TableCellStrategy,
    TargetSpec,
    TextStrategy,
)
from cua.core.text import normalize, route_of

from .base import (
    ActionName,
    DialogInfo,
    Observation,
    ObservedElement,
    Resolution,
    ResolutionError,
    StrategyAttempt,
)

DOM_JS = (Path(__file__).parent / "dom.js").read_text(encoding="utf-8")

# Reports what a *person* does in the window during a handoff (clicks and edits only; no
# keystrokes, and never the value of a sensitive field). Runs in every frame.
HUMAN_JS = """
(() => {
  if (window.__cuaHumanHooked) return;
  window.__cuaHumanHooked = true;
  const CONTROL = 'button, a, input, select, textarea, [role=button], [role=link]';
  const report = (kind, ev) => {
    try {
      if (!ev.isTrusted || !window.__cua || !window.__cuaHumanEvent) return;
      const el = ev.target && ev.target.closest ? ev.target.closest(CONTROL) : null;
      if (!el) return;
      const d = window.__cua.describe(el);
      const out = {event: kind, role: d.role, name: d.name, label: d.label, tag: d.tag,
                   route: location.pathname, title: document.title};
      if (kind === 'change') {
        out.value = d.sensitive ? '[not recorded]'
          : (el.tagName === 'SELECT' ? (el.selectedOptions[0] || {}).text || ''
          : (el.type === 'checkbox' || el.type === 'radio') ? String(el.checked) : el.value);
      }
      window.__cuaHumanEvent(out);
    } catch (e) { /* never break the page */ }
  };
  document.addEventListener('click', (e) => report('click', e), true);
  document.addEventListener('change', (e) => report('change', e), true);
})();
"""
CHROMIUM_ENV = "CUA_CHROMIUM_PATH"

# Attribute values that look generated (ctl00_main_txtMid_3fa9c1, react-17, :r3:) are never
# offered as locators.
_UNSTABLE = ("_[0-9a-f]{5,}$", r"\d{4,}", "^:r", "^[0-9a-f]{8,}$")


class NavigationBlocked(Exception):
    def __init__(self, url: str) -> None:
        super().__init__(f"navigation blocked by policy: {url}")
        self.url = url


class UnsupportedStrategy(Exception):
    pass


def _stable(value: str) -> bool:
    return bool(value) and not any(re.search(rx, value) for rx in _UNSTABLE)


def _rect(box: Any) -> tuple[int, int, int, int] | None:
    """Playwright bounding box -> (x, y, width, height) in page pixels."""
    if not box:
        return None
    return (int(box["x"]), int(box["y"]), int(box["width"]), int(box["height"]))


class WebSurface:
    def __init__(
        self,
        context: BrowserContext,
        base_url: str,
        *,
        content_frame: tuple[FrameRef, ...] = (),
        url_guard: Callable[[str], bool] | None = None,
        action_timeout_ms: int = 5000,
        settle_ms: int = 5000,
    ) -> None:
        self.context = context
        self.base_url = base_url.rstrip("/") + "/"
        self.content_frame = content_frame
        self.url_guard = url_guard
        self.blocked: list[str] = []
        self.native_dialogs: list[DialogInfo] = []
        self._pending_docs = 0
        self._last_nav = time.monotonic()
        self._refs: dict[str, tuple[Frame, ElementHandle, dict[str, Any]]] = {}

        self.settle_ms = settle_ms
        # Called before every automation action. The engines set it to the control-lease check,
        # so automation cannot act while a person holds the session.
        self.gate: Callable[[], None] | None = None
        # Set by the handoff while a person drives; receives their clicks and edits.
        self.on_human_event: Callable[[dict[str, Any]], None] | None = None
        context.set_default_timeout(action_timeout_ms)
        context.add_init_script(DOM_JS)
        context.expose_binding("__cuaHumanEvent", self._on_human)
        context.add_init_script(HUMAN_JS)
        context.route("**/*", self._on_route)
        self.page: Page = context.new_page()
        self.page.on("dialog", self._on_dialog)
        self.page.on("request", self._on_request)
        self.page.on("requestfinished", self._on_request_done)
        self.page.on("requestfailed", self._on_request_done)
        self.page.on("framenavigated", lambda _f: self._touch())

    # --- lifecycle --------------------------------------------------------------------------

    @classmethod
    def open(
        cls,
        browser: Browser,
        base_url: str,
        *,
        extra_headers: dict[str, str] | None = None,
        headless_viewport: tuple[int, int] = (1280, 800),
        **kw: Any,
    ) -> WebSurface:
        ctx = browser.new_context(
            viewport={"width": headless_viewport[0], "height": headless_viewport[1]},
            extra_http_headers=extra_headers or {},
        )
        return cls(ctx, base_url, **kw)

    def start(self, route: str = "/") -> None:
        self.page.goto(urljoin(self.base_url, route.lstrip("/")), wait_until="load")
        self.settle(self.settle_ms)

    def close(self) -> None:
        self._dispose_refs()
        self.context.close()

    # --- event plumbing ---------------------------------------------------------------------

    def _touch(self) -> None:
        self._last_nav = time.monotonic()

    def _on_route(self, route: Route, request: Request) -> None:
        if (
            self.url_guard is not None
            and request.is_navigation_request()
            and not self.url_guard(request.url)
        ):
            self.blocked.append(request.url)
            route.abort("blockedbyclient")
            return
        route.continue_()

    def _on_request(self, request: Request) -> None:
        if request.resource_type == "document":
            self._pending_docs += 1
            self._touch()

    def _on_request_done(self, request: Request) -> None:
        if request.resource_type == "document":
            self._pending_docs = max(0, self._pending_docs - 1)
            self._touch()

    def _on_human(self, _source: Any, event: Any) -> None:
        if self.on_human_event is not None and isinstance(event, dict):
            self.on_human_event({k: str(v)[:200] for k, v in event.items()})

    def bring_to_front(self) -> None:
        with contextlib.suppress(PlaywrightError):
            self.page.bring_to_front()

    def _on_dialog(self, dialog: Dialog) -> None:
        self.native_dialogs.append(
            DialogInfo(
                kind="native",
                title=dialog.message.split("\n")[0][:120],
                text=dialog.message[:400],
                buttons=("OK",) if dialog.type == "alert" else ("OK", "Cancel"),
            )
        )
        if dialog.type == "alert":
            dialog.accept()
        else:
            dialog.dismiss()

    # --- frames -----------------------------------------------------------------------------

    def frame(self, path: tuple[FrameRef, ...] = ()) -> Frame:
        current = self.page.main_frame
        for hop in path:
            match = [
                f
                for f in current.child_frames
                if (hop.name is not None and f.name == hop.name)
                or (hop.url_contains is not None and hop.url_contains in f.url)
            ]
            if len(match) != 1:
                raise LookupError(f"frame {hop.model_dump(exclude_none=True)} not found")
            current = match[0]
        return current

    def _frame_path(self, frame: Frame) -> tuple[FrameRef, ...]:
        hops: list[FrameRef] = []
        f: Frame | None = frame
        while f is not None and f.parent_frame is not None:
            hops.append(FrameRef(name=f.name) if f.name else FrameRef(url_contains=route_of(f.url)))
            f = f.parent_frame
        return tuple(reversed(hops))

    def _content(self) -> Frame:
        try:
            return self.frame(self.content_frame)
        except LookupError:
            return self.page.main_frame

    @property
    def content_url(self) -> str:
        """URL of the frame that is "the page" (the content frame in a frameset)."""
        return self._content().url

    def _lib(self, frame: Frame) -> Frame:
        if not frame.evaluate("() => !!window.__cua"):
            frame.evaluate(DOM_JS)
        return frame

    def _js_elements(self, frame: Frame, fn: str, *args: Any) -> list[ElementHandle]:
        self._lib(frame)
        arr = frame.evaluate_handle(
            "(a) => window.__cua[a.fn](...a.args)", {"fn": fn, "args": list(args)}
        )
        try:
            props = arr.get_properties()
            items = sorted(
                ((int(k), v) for k, v in props.items() if k.isdigit()), key=lambda kv: kv[0]
            )
            out = []
            for _, h in items:
                el = h.as_element()
                if el is not None:
                    out.append(el)
                else:
                    h.dispose()
            return out
        finally:
            arr.dispose()

    # --- locating ---------------------------------------------------------------------------

    def _candidates(self, frame: Frame, s: Strategy) -> list[ElementHandle]:
        if isinstance(s, RoleStrategy):
            try:
                handles = frame.get_by_role(s.role, name=s.name, exact=s.exact).element_handles()  # type: ignore[arg-type]
            except PlaywrightError:
                return []
            if s.near_text and len(handles) > 1:
                self._lib(frame)
                depths: list[int] = frame.evaluate(
                    "(a) => window.__cua.nearDepth(a.els, a.near)",
                    {"els": handles, "near": s.near_text},
                )
                best = min(depths)
                keep = [h for h, d in zip(handles, depths, strict=True) if d == best and best < 1e9]
                for h in handles:
                    if h not in keep:
                        h.dispose()
                return keep
            return handles
        if isinstance(s, LabelStrategy):
            return self._js_elements(frame, "byLabel", s.text, s.control)
        if isinstance(s, TableCellStrategy):
            return self._js_elements(
                frame, "byTableCell", s.table_near, s.row_match, s.column, s.row_match_mode
            )
        if isinstance(s, AttributeStrategy):
            return self._js_elements(frame, "byAttribute", s.attr, s.value, s.tag)
        if isinstance(s, TextStrategy):
            return self._js_elements(frame, "byText", s.text, s.exact)
        if isinstance(s, CssStrategy):
            return self._js_elements(frame, "byCss", s.selector)
        raise UnsupportedStrategy(f"{s.by} is not supported on the web surface")

    def resolve(self, target: TargetSpec) -> Resolution:
        attempts: list[StrategyAttempt] = []
        try:
            frame = self.frame(target.frame)
        except LookupError as exc:
            attempts = [StrategyAttempt(s.by, 0, str(exc)) for s in target.strategies]
            raise ResolutionError(target, tuple(attempts)) from exc
        for i, s in enumerate(target.strategies):
            try:
                handles = self._candidates(frame, s)
            except UnsupportedStrategy as exc:
                attempts.append(StrategyAttempt(s.by, 0, str(exc)))
                continue
            attempts.append(StrategyAttempt(s.by, len(handles)))
            if len(handles) == 1:
                return Resolution(target, i, (frame, handles[0]), tuple(attempts))
            for h in handles:
                h.dispose()
        raise ResolutionError(target, tuple(attempts))

    def same_element(self, a: Resolution, b: Resolution) -> bool:
        (fa, ea), (fb, eb) = a.handle, b.handle
        return fa == fb and bool(fa.evaluate("(p) => p.x === p.y", {"x": ea, "y": eb}))

    def count(self, target: TargetSpec) -> int:
        try:
            frame = self.frame(target.frame)
        except LookupError:
            return 0
        for s in target.strategies:
            try:
                handles = self._candidates(frame, s)
            except UnsupportedStrategy:
                continue
            n = len(handles)
            for h in handles:
                h.dispose()
            if n:
                return n
        return 0

    # --- acting and reading -----------------------------------------------------------------

    def act(self, res: Resolution, action: ActionName, value: str | None = None) -> None:
        if self.gate is not None:
            self.gate()
        _frame, el = res.handle
        self.native_dialogs.clear()
        if action == "click":
            el.click()
        elif action == "fill":
            el.fill(value or "")
        elif action == "check":
            el.check()
        elif action == "select":
            options: list[dict[str, str]] = el.evaluate(
                "(e) => [...e.options].map(o => ({v: o.value, t: o.text}))"
            )
            want = normalize(value or "")
            match = next((o for o in options if normalize(o["t"]) == want), None) or next(
                (o for o in options if o["v"] == value), None
            )
            if match is None and value:  # e.g. an account number inside "Checking - 0042-..."
                inside = [o for o in options if value in o["t"]]
                match = inside[0] if len(inside) == 1 else None
            if match is None:
                raise ValueError(f"option {value!r} not in {[o['t'] for o in options]}")
            el.select_option(value=match["v"])
        elif action == "press":
            el.press(value or "Enter")
        else:  # pragma: no cover - ActionName is closed
            raise ValueError(action)
        self.settle(self.settle_ms)

    def read(self, res: Resolution) -> str:
        frame, el = res.handle
        self._lib(frame)
        return str(el.evaluate("(e) => window.__cua.readValue(e)"))

    def navigate(self, route: str, frame: tuple[FrameRef, ...] = ()) -> None:
        if self.gate is not None:
            self.gate()
        url = urljoin(self.base_url, route.lstrip("/"))
        self.native_dialogs.clear()
        try:
            self.frame(frame).goto(url, wait_until="load")
        except PlaywrightError as exc:
            if url in self.blocked or "ERR_BLOCKED_BY_CLIENT" in str(exc):
                raise NavigationBlocked(url) from exc
            raise
        self.settle(self.settle_ms)

    def control_name(self, res: Resolution) -> str:
        """The name a person would read for a resolved control (for policy risk rules)."""
        frame, el = res.handle
        self._lib(frame)
        return str(
            el.evaluate("(e) => window.__cua.nameOf(e) || (window.__cua.labelsOf(e)[0] || '')")
        )

    def alerts(self) -> list[str]:
        """Visible application messages (error/warning banners, field errors)."""
        out: list[str] = []
        for f in self.page.frames:
            try:
                out += self._lib(f).evaluate("() => window.__cua.alerts()")
            except PlaywrightError:
                continue
        return out[:5]

    def pause(self, ms: int) -> None:
        self.page.wait_for_timeout(ms)

    def settle(self, timeout_ms: int = 5000) -> None:
        """Wait until no document is loading and navigation has been quiet for 150 ms.

        Legacy apps often never reach network idle, so this has a hard ceiling and never raises.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        self.page.wait_for_timeout(50)
        while time.monotonic() < deadline:
            quiet = time.monotonic() - self._last_nav >= 0.15
            if self._pending_docs == 0 and quiet:
                break
            self.page.wait_for_timeout(25)
        for f in self.page.frames:
            remaining = max(1, int((deadline - time.monotonic()) * 1000))
            with contextlib.suppress(PlaywrightError):
                f.wait_for_load_state("load", timeout=remaining)

    # --- state for conditions ---------------------------------------------------------------

    def _frame_infos(self) -> list[tuple[Frame, dict[str, Any]]]:
        infos = []
        for f in self.page.frames:
            try:
                infos.append((f, self._lib(f).evaluate("() => window.__cua.pageInfo()")))
            except PlaywrightError:
                continue  # detached or cross-origin frame
        return infos

    def page_state(
        self, outputs: dict[str, Any] | None = None, fired: frozenset[str] = frozenset()
    ) -> PageState:
        content = self._content()
        infos = self._frame_infos()
        by_frame = {f: i for f, i in infos}
        main = by_frame.get(content, {"title": "", "text": "", "headings": [], "dialogs": []})
        dialogs = [d["title"] for _, i in infos for d in i["dialogs"]]
        dialogs += [d.title for d in self.native_dialogs]
        return PageState(
            route=route_of(content.url),
            title=main["title"],
            visible_text="\n".join(i["text"] for _, i in infos if i["text"]),
            headings=tuple(h for _, i in infos for h in i["headings"]),
            dialog_titles=tuple(dialogs),
            outputs=dict(outputs or {}),
            fired_detectors=fired,
            element_probe=lambda t: self.count(t) > 0,
        )

    def dialogs(self) -> tuple[DialogInfo, ...]:
        found = [
            DialogInfo("modal", d["title"], d["text"], tuple(d["buttons"]))
            for _, i in self._frame_infos()
            for d in i["dialogs"]
        ]
        return tuple(found + self.native_dialogs)

    # --- observation for the discovery agent ------------------------------------------------

    def _dispose_refs(self) -> None:
        for _f, h, _i in self._refs.values():
            with contextlib.suppress(PlaywrightError):
                h.dispose()
        self._refs.clear()

    def observe(self) -> Observation:
        self._dispose_refs()
        elements: list[ObservedElement] = []
        n = 0
        for f in self.page.frames:
            try:
                handles = self._js_elements(f, "observe")
            except PlaywrightError:
                continue
            path = self._frame_path(f)
            for h in handles:
                info: dict[str, Any] = h.evaluate("(e) => window.__cua.describe(e)")
                n += 1
                ref = f"e{n}"
                self._refs[ref] = (f, h, info)
                elements.append(
                    ObservedElement(
                        ref=ref,
                        role=info["role"],
                        name=info["name"],
                        label=info["label"],
                        tag=info["tag"],
                        frame=path,
                        value="" if info["sensitive"] else info["value"],
                        options=tuple(info["options"]),
                        row=tuple(info["row"]),
                        column=info["column"],
                        context=info["context"],
                        dialog=info["dialog"],
                        disabled=info["disabled"],
                        sensitive=info["sensitive"],
                        bbox=_rect(h.bounding_box()),
                    )
                )
        content = self._content()
        info = self._lib(content).evaluate("() => window.__cua.pageInfo()")
        return Observation(
            url=content.url,
            route=route_of(content.url),
            title=info["title"],
            frames=tuple(f.name or route_of(f.url) for f in self.page.frames),
            elements=tuple(elements),
            text=info["text"],
            dialogs=self.dialogs(),
            blocked_navigations=tuple(self.blocked),
        )

    def resolution_for_ref(self, ref: str) -> Resolution:
        frame, handle, info = self._refs[ref]
        target = TargetSpec(
            description=f"observed element {ref} ({info['role']} {info['label'] or info['name']})",
            frame=self._frame_path(frame),
            strategies=(CssStrategy(selector="*"),),
        )
        return Resolution(target, 0, (frame, handle), ())

    def fingerprint(self, ref: str) -> dict[str, Any]:
        _frame, handle, info = self._refs[ref]
        attrs = {k: v for k, v in info["attrs"].items() if k in ("name", "type") and _stable(v)}
        return {
            "tag": info["tag"],
            "role": info["role"],
            "name": info["name"] or None,
            "near_text": info["label"] or None,
            "row_context": tuple(info["row"]),
            "attributes": attrs,
            "bbox": _rect(handle.bounding_box()),
        }

    def suggest_target(self, ref: str, description: str) -> TargetSpec:
        """Propose strategies for an element the agent used, keeping only those that resolve
        *uniquely to that same element right now*. Ordered most robust first.
        """
        frame, handle, info = self._refs[ref]
        ctx: dict[str, Any] = handle.evaluate("(e) => window.__cua.suggestContext(e)")
        role, name, label = info["role"], info["name"], info["label"]
        proposals: list[Strategy] = []
        if role not in ("cell", "link") and name:
            proposals.append(RoleStrategy(role=role, name=name))
            if ctx["nearTitle"]:
                proposals.append(RoleStrategy(role=role, name=name, near_text=ctx["nearTitle"]))
        if role == "link" and name:
            proposals.append(RoleStrategy(role="link", name=name))
        if label:
            control = "value_cell" if role == "cell" else role if role != "link" else "any"
            if control in ("textbox", "checkbox", "combobox", "button", "value_cell"):
                proposals.append(LabelStrategy(text=label, control=control))
        if ctx["tableCell"]:
            proposals.append(TableCellStrategy(**ctx["tableCell"]))
        attr_name = info["attrs"]["name"]
        if _stable(attr_name):
            proposals.append(AttributeStrategy(attr="name", value=attr_name, tag=info["tag"]))
        # Text locators only for links: a data cell's text is the data itself ("Maria Delgado")
        # and changes with every input, so it can never be a durable locator.
        if role == "link" and ctx["text"] and len(ctx["text"]) <= 60:
            proposals.append(TextStrategy(text=ctx["text"]))

        verified: list[Strategy] = []
        seen_kinds: set[str] = set()
        for s in proposals:
            if s.by in seen_kinds:
                continue
            cands = self._candidates(frame, s)
            same = len(cands) == 1 and bool(
                frame.evaluate("(a) => a.x === a.y", {"x": cands[0], "y": handle})
            )
            for c in cands:
                c.dispose()
            if same:
                verified.append(s)
                seen_kinds.add(s.by)
        if not verified:
            raise ResolutionError(
                TargetSpec(description=description, strategies=(CssStrategy(selector="*"),)),
                (StrategyAttempt("suggest", 0, "no proposed strategy is unique"),),
            )
        return TargetSpec(
            description=description,
            frame=self._frame_path(frame),
            strategies=tuple(verified),
        )

    # --- evidence ---------------------------------------------------------------------------

    def screenshot_png(
        self,
        *,
        redact_values: tuple[str, ...] = (),
        redact_patterns: tuple[str, ...] = (),
        mask: tuple[TargetSpec, ...] = (),
    ) -> bytes:
        """Viewport PNG with sensitive regions already blacked out (nothing raw leaves here)."""
        from PIL import Image

        boxes: list[tuple[int, int, int, int]] = []
        for f in self.page.frames:
            try:
                for h in self._js_elements(
                    f, "maskTargets", list(redact_values), list(redact_patterns)
                ):
                    r = _rect(h.bounding_box())
                    if r:
                        boxes.append(r)
                    h.dispose()
            except PlaywrightError:
                continue
        for t in mask:
            try:
                res = self.resolve(t)
            except ResolutionError:
                continue
            r = _rect(res.handle[1].bounding_box())
            if r:
                boxes.append(r)
        img = Image.open(io.BytesIO(self.page.screenshot())).convert("RGB")
        black_out(img, boxes)
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()

    def screenshot(
        self,
        path: str,
        *,
        redact_values: tuple[str, ...] = (),
        redact_patterns: tuple[str, ...] = (),
        mask: tuple[TargetSpec, ...] = (),
    ) -> str:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(
            self.screenshot_png(
                redact_values=redact_values, redact_patterns=redact_patterns, mask=mask
            )
        )
        return path


def black_out(img: Any, boxes: list[tuple[int, int, int, int]]) -> None:
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    for x, y, bw, bh in boxes:
        draw.rectangle([x - 2, y - 2, x + bw + 2, y + bh + 2], fill=(0, 0, 0))


@contextmanager
def launch_browser(*, headless: bool = True) -> Iterator[Browser]:
    """Chromium from Playwright's cache, or from ``CUA_CHROMIUM_PATH`` if set."""
    exe = os.environ.get(CHROMIUM_ENV) or None
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, executable_path=exe)
        try:
            yield browser
        finally:
            browser.close()
