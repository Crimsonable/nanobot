from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
from dataclasses import MISSING, asdict, dataclass, fields
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.async_api import BrowserContext, Page, async_playwright

LOGIN_URL = "https://cdyyrz.powerchina.cn/"
MEETING_URL = (
    "https://ump.chidi.com.cn:8098/seeyon/collaboration/"
    "collaboration.do?method=newColl&from=templateNewColl"
    "&templateId=-8318028373217228838&showTab=true"
)
ROOT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_STATE_PATH = ROOT_DIR / "state.json"
JS_PATH_OP = ROOT_DIR / "operate.js"
JS_PATH_PHONE = ROOT_DIR / "operate_phone.js"

LEADER_FLAG_LABELS = {True: "有", False: "无"}
ALLOW_PHONE_LABELS = {True: "是", False: "否"}
OFFICE_AREA_OPTIONS = ["成都", "温江", "研发中心"]
SLOT_OPTIONS = [
    "上午",
    "下午",
    "全天(9:00-17:00)",
    "晚上",
]

logger = logging.getLogger(__name__)


class BrowserInitFailed(RuntimeError):
    pass


class ReservationError(RuntimeError):
    pass


class MissingFieldsError(ValueError):
    def __init__(
        self, missing_fields: list[str], *, unknown_fields: list[str] | None = None
    ):
        self.missing_fields = missing_fields
        self.unknown_fields = unknown_fields or []

        parts: list[str] = []
        if self.missing_fields:
            parts.append(f"missing required fields: {', '.join(self.missing_fields)}")
        if self.unknown_fields:
            parts.append(f"unknown fields: {', '.join(self.unknown_fields)}")
        super().__init__("; ".join(parts))


@dataclass
class LoginForm:
    usrname: str
    pwd: str


@dataclass
class IdleRoomQuery:
    office_area: int
    date: str
    start_time: str
    end_time: str
    slot: int


@dataclass
class MeetingDetails:
    phone: str
    meeting_name: str
    office_area: int
    date: str
    slot: int
    start_time: str
    end_time: str
    headcount: str
    leader_flag: bool


@dataclass
class ReservationOptions:
    selected_room: str = ""
    allow_phone: bool = False


@dataclass
class ReserveRequest:
    meeting: MeetingDetails
    reservation: ReservationOptions


class BrowserAgent:
    def __init__(
        self, *, headless: bool = True, state_path: Path | None = DEFAULT_STATE_PATH
    ):
        self.headless = headless
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.state_path = state_path

    async def __aenter__(self) -> "BrowserAgent":
        for proxy_var in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            os.environ.pop(proxy_var, None)

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--no-proxy-server",
                "--proxy-server=direct://",
                "--proxy-bypass-list=*",
            ],
        )
        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 900},
            storage_state=(
                str(self.state_path)
                if self.state_path and self.state_path.exists()
                else None
            ),
        )
        self.page = await self.context.new_page()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.page:
            await self.page.close()
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()


def _is_missing_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def _field_default_value(field: Any) -> Any:
    if field.default is not MISSING:
        return field.default
    if field.default_factory is not MISSING:
        return field.default_factory()
    return MISSING


def _dataclass_from_payload(
    model_cls: type[Any],
    payload: dict[str, Any],
    *,
    require_all_non_default: bool = False,
    allow_default_fields: set[str] | None = None,
) -> Any:
    values: dict[str, Any] = {}
    missing: list[str] = []
    valid_fields = {field.name: field for field in fields(model_cls)}
    allow_default_fields = allow_default_fields or set()

    for name, field in valid_fields.items():
        has_value = name in payload and not _is_missing_value(payload[name])
        default_value = _field_default_value(field)
        strict_required = require_all_non_default and name not in allow_default_fields

        if has_value:
            values[name] = payload[name]
            if (
                strict_required
                and default_value is not MISSING
                and payload[name] == default_value
            ):
                missing.append(name)
            continue

        if strict_required or default_value is MISSING:
            missing.append(name)

    extra_keys = sorted(set(payload) - set(valid_fields))
    if missing or extra_keys:
        raise MissingFieldsError(missing, unknown_fields=extra_keys)

    return model_cls(**values)


def _prefix_error(error: MissingFieldsError, prefix: str) -> MissingFieldsError:
    return MissingFieldsError(
        [f"{prefix}.{field_name}" for field_name in error.missing_fields],
        unknown_fields=[
            f"{prefix}.{field_name}" for field_name in error.unknown_fields
        ],
    )


def _merge_missing_errors(*errors: MissingFieldsError) -> MissingFieldsError:
    missing_fields: list[str] = []
    unknown_fields: list[str] = []
    for error in errors:
        missing_fields.extend(error.missing_fields)
        unknown_fields.extend(error.unknown_fields)
    return MissingFieldsError(
        sorted(dict.fromkeys(missing_fields)),
        unknown_fields=sorted(dict.fromkeys(unknown_fields)),
    )


def _load_json_arg(raw_json: str | None, json_file: str | None) -> dict[str, Any]:
    if raw_json and json_file:
        raise ValueError("Only one of inline JSON or JSON file may be provided.")
    if json_file:
        return json.loads(Path(json_file).read_text(encoding="utf-8"))
    if raw_json:
        return json.loads(raw_json)
    return {}


def _parse_reserve_request(payload: dict[str, Any]) -> ReserveRequest:
    meeting_payload = payload.get("meeting")
    reservation_payload = payload.get("reservation", {})

    missing_sections: list[str] = []
    unknown_sections = sorted(set(payload) - {"meeting", "reservation"})
    if not isinstance(meeting_payload, dict):
        missing_sections.append("meeting")
    if "reservation" in payload and not isinstance(reservation_payload, dict):
        missing_sections.append("reservation")

    if missing_sections or unknown_sections:
        raise MissingFieldsError(
            missing_sections,
            unknown_fields=unknown_sections,
        )

    errors: list[MissingFieldsError] = []
    meeting = None
    reservation = None

    try:
        meeting = _dataclass_from_payload(
            MeetingDetails,
            meeting_payload,
            require_all_non_default=True,
        )
    except MissingFieldsError as exc:
        errors.append(_prefix_error(exc, "meeting"))

    try:
        reservation = _dataclass_from_payload(
            ReservationOptions,
            reservation_payload,
            allow_default_fields={"selected_room", "allow_phone"},
        )
    except MissingFieldsError as exc:
        errors.append(_prefix_error(exc, "reservation"))

    if errors:
        raise _merge_missing_errors(*errors)

    return ReserveRequest(meeting=meeting, reservation=reservation)


def _require_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{field_name} must be a boolean value.")


def _require_index(value: Any, options: list[str], field_name: str) -> str:
    if not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer index.")
    if value < 0 or value >= len(options):
        raise ValueError(
            f"{field_name} index out of range. valid range: 0-{len(options) - 1}"
        )
    return options[value]


def _render_js(path: Path, payload: dict[str, Any] | Any) -> str:
    js = path.read_text(encoding="utf-8")
    if not isinstance(payload, dict):
        payload = asdict(payload)
    for key, value in payload.items():
        safe_value = "" if value is None else str(value)
        js = re.sub(f"%{key}%", safe_value, js)
    return js


def _normalize_idle_room_query(query: IdleRoomQuery) -> dict[str, Any]:
    return {
        "office_area": _require_index(
            query.office_area, OFFICE_AREA_OPTIONS, "office_area"
        ),
        "date": query.date,
        "slot": _require_index(query.slot, SLOT_OPTIONS, "slot"),
        "start_time": query.start_time,
        "end_time": query.end_time,
        "phone": "",
        "meeting_name": "",
        "headcount": "",
        "leader_flag": "",
    }


def _normalize_meeting_details(meeting: MeetingDetails) -> dict[str, Any]:
    return {
        "phone": meeting.phone,
        "meeting_name": meeting.meeting_name,
        "office_area": _require_index(
            meeting.office_area, OFFICE_AREA_OPTIONS, "meeting.office_area"
        ),
        "date": meeting.date,
        "slot": _require_index(meeting.slot, SLOT_OPTIONS, "meeting.slot"),
        "start_time": meeting.start_time,
        "end_time": meeting.end_time,
        "headcount": meeting.headcount,
        "leader_flag": LEADER_FLAG_LABELS[
            _require_bool(meeting.leader_flag, "meeting.leader_flag")
        ],
    }


def _extract_room_candidates(idle_rooms: Any) -> list[dict[str, Any]]:
    if isinstance(idle_rooms, dict):
        data = idle_rooms.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    if isinstance(idle_rooms, list):
        return [item for item in idle_rooms if isinstance(item, dict)]
    return []


def _room_display_name(room: dict[str, Any]) -> str:
    for key in ("field0001", "meeting_room", "room_name", "name"):
        value = room.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in room.values():
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _pick_room(idle_rooms: Any, preferred_room: str = "") -> dict[str, Any]:
    candidates = _extract_room_candidates(idle_rooms)
    if not candidates:
        raise ReservationError("No available meeting rooms were returned.")

    if preferred_room:
        preferred = preferred_room.strip().lower()
        for room in candidates:
            if _room_display_name(room).lower() == preferred:
                return room
        raise ReservationError(
            f"Requested room not found in available rooms: {preferred_room}"
        )

    return candidates[0]


async def save_state_storage(
    ctx: BrowserContext, state_path: Path = DEFAULT_STATE_PATH
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    await ctx.storage_state(path=str(state_path))


async def fill_login_form_by_locator(page: Page, form: LoginForm) -> None:
    await page.goto(LOGIN_URL)
    await page.set_viewport_size({"width": 1280, "height": 900})
    await page.wait_for_load_state("networkidle")

    if urlparse(page.url).hostname != urlparse(LOGIN_URL).hostname:
        logger.info("Login state is already valid.")
        return

    await page.get_by_text("用户名密码登录", exact=True).click()
    await page.locator("#j_username").fill(form.usrname)
    await page.locator("#j_password").fill(form.pwd)
    await page.get_by_role("button", name="登录").click()
    await page.wait_for_load_state("networkidle")


async def open_meeting_form(page: Page) -> None:
    await page.goto(MEETING_URL)
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(1500)


async def fill_meeting_booking_form(
    page: Page, meeting: MeetingDetails | IdleRoomQuery
) -> None:
    payload = (
        _normalize_meeting_details(meeting)
        if isinstance(meeting, MeetingDetails)
        else _normalize_idle_room_query(meeting)
    )
    await page.evaluate(_render_js(JS_PATH_OP, payload))


async def get_idle_room(page: Page) -> Any:
    frame = page.frame(name="zwIframe")
    if not frame:
        raise RuntimeError("zwIframe not found")

    def predicate(resp) -> bool:
        if "/seeyon/ajax.do?method=ajaxAction&managerName=formManager" not in resp.url:
            return False
        if resp.request.method != "POST":
            return False

        post_data = resp.request.post_data or ""
        params = parse_qs(post_data)
        return params.get("managerMethod", [None])[0] == "getFormMasterDataListByFormId"

    async with page.expect_response(predicate, timeout=15000) as resp_info:
        await frame.locator(".ico16.correlation_form_16").click()

    resp = await resp_info.value
    return await resp.json()


async def select_and_submit(
    page: Page,
    selected_room_name: str,
    *,
    allow_phone: bool,
) -> None:
    frame = page.frame(name="layui-layer-iframe1")
    if frame is None:
        raise ReservationError("Room selection frame was not found.")

    room_locator = frame.get_by_text(selected_room_name, exact=True).first
    if await room_locator.count() == 0:
        raise ReservationError(
            f"Room '{selected_room_name}' was not found in the selection frame."
        )
    await room_locator.click()
    await page.wait_for_timeout(1500)
    await page.get_by_text("确定").click()
    await page.wait_for_timeout(2000)

    await page.evaluate(
        _render_js(
            JS_PATH_PHONE,
            {
                "phone_flag": ALLOW_PHONE_LABELS[
                    _require_bool(allow_phone, "reservation.allow_phone")
                ]
            },
        )
    )

    await page.wait_for_timeout(3000)
    await page.locator("#sendId_a").click()


async def login(
    login_info: LoginForm,
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    headless: bool = True,
) -> dict[str, Any]:
    async with BrowserAgent(headless=headless, state_path=None) as browser:
        if not browser.page or not browser.context:
            raise BrowserInitFailed("Playwright browser not initialized.")
        await fill_login_form_by_locator(browser.page, login_info)
        await save_state_storage(browser.context, state_path=state_path)

    return {"ok": True, "state_path": str(state_path)}


async def get_idle(
    meeting_info: IdleRoomQuery,
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    headless: bool = True,
) -> dict[str, Any]:
    async with BrowserAgent(state_path=state_path, headless=headless) as browser:
        if not browser.page:
            raise BrowserInitFailed("Playwright browser not initialized.")
        await open_meeting_form(browser.page)
        await fill_meeting_booking_form(browser.page, meeting_info)
        await browser.page.wait_for_timeout(2000)
        result = await get_idle_room(browser.page)
    return {"ok": True, "state_path": str(state_path), "idle_rooms": result}


async def reserve(
    login_info: LoginForm,
    reserve_request: ReserveRequest,
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    headless: bool = True,
) -> dict[str, Any]:
    async with BrowserAgent(headless=headless, state_path=None) as browser:
        if not browser.page or not browser.context:
            raise BrowserInitFailed("Playwright browser not initialized.")

        await fill_login_form_by_locator(browser.page, login_info)
        await save_state_storage(browser.context, state_path=state_path)
        await open_meeting_form(browser.page)
        await fill_meeting_booking_form(browser.page, reserve_request.meeting)
        await browser.page.wait_for_timeout(2000)

        idle_rooms = await get_idle_room(browser.page)
        selected_room = _pick_room(
            idle_rooms,
            reserve_request.reservation.selected_room,
        )
        selected_room_name = _room_display_name(selected_room)
        if not selected_room_name:
            raise ReservationError(
                "Failed to determine the selected meeting room name."
            )

        await select_and_submit(
            browser.page,
            selected_room_name,
            allow_phone=reserve_request.reservation.allow_phone,
        )
        return {
            "ok": True,
            "state_path": str(state_path),
            "selected_room": selected_room_name,
            "allow_phone": reserve_request.reservation.allow_phone,
            "idle_rooms": idle_rooms,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Login, query idle meeting rooms, or complete a reservation."
    )
    parser.add_argument(
        "--state-path", default=str(DEFAULT_STATE_PATH), help="Storage state JSON path."
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode. Defaults to headed for manual login/debugging.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Python logging level.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    login_parser = subparsers.add_parser(
        "login", help="Only perform login and save state."
    )
    login_parser.add_argument(
        "--login-json", help='Inline login JSON, e.g. \'{"usrname":"u","pwd":"p"}\'.'
    )
    login_parser.add_argument("--login-file", help="Path to a login JSON file.")

    idle_parser = subparsers.add_parser(
        "get-idle", help="Query idle meeting rooms with existing state."
    )
    idle_parser.add_argument(
        "--meeting-json",
        help="Inline query JSON. Required fields: office_area(index), date, slot(index), start_time, end_time.",
    )
    idle_parser.add_argument("--meeting-file", help="Path to a query JSON file.")

    reserve_parser = subparsers.add_parser(
        "reserve", help="Login, fill the reservation form, select a room, and submit."
    )
    reserve_parser.add_argument(
        "--login-json", help='Inline login JSON, e.g. \'{"usrname":"u","pwd":"p"}\'.'
    )
    reserve_parser.add_argument("--login-file", help="Path to a login JSON file.")
    reserve_parser.add_argument(
        "--meeting-json",
        help='Inline reserve JSON. Shape: {"meeting": {...}, "reservation": {...}}.',
    )
    reserve_parser.add_argument("--meeting-file", help="Path to a reserve JSON file.")

    return parser


async def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    state_path = Path(args.state_path).expanduser().resolve()

    if args.command == "login":
        login_payload = _load_json_arg(args.login_json, args.login_file)
        login_form = _dataclass_from_payload(LoginForm, login_payload)
        return await login(login_form, state_path=state_path, headless=args.headless)

    if args.command == "get-idle":
        meeting_payload = _load_json_arg(args.meeting_json, args.meeting_file)
        meeting_form = _dataclass_from_payload(
            IdleRoomQuery,
            meeting_payload,
            require_all_non_default=True,
        )
        return await get_idle(
            meeting_form, state_path=state_path, headless=args.headless
        )

    if args.command == "reserve":
        login_payload = _load_json_arg(args.login_json, args.login_file)
        reserve_payload = _load_json_arg(args.meeting_json, args.meeting_file)

        errors: list[MissingFieldsError] = []
        login_form = None
        reserve_request = None

        try:
            login_form = _dataclass_from_payload(LoginForm, login_payload)
        except MissingFieldsError as exc:
            errors.append(_prefix_error(exc, "login"))

        try:
            reserve_request = _parse_reserve_request(reserve_payload)
        except MissingFieldsError as exc:
            errors.append(exc)

        if errors:
            raise _merge_missing_errors(*errors)

        return await reserve(
            login_form,
            reserve_request,
            state_path=state_path,
            headless=args.headless,
        )

    raise ValueError(f"Unsupported command: {args.command}")


async def debug_reserve_example() -> dict[str, Any]:
    login_form = LoginForm(
        usrname="2021088",
        pwd="jF70wO49",
    )
    reserve_request = ReserveRequest(
        meeting=MeetingDetails(
            phone="18200000000",
            meeting_name="project-sync",
            office_area=0,
            date="2026-03-12",
            slot=0,
            start_time="2026-03-12 09:00",
            end_time="2026-03-12 10:00",
            headcount="12",
            leader_flag=False,
        ),
        reservation=ReservationOptions(
            selected_room="",
            allow_phone=False,
        ),
    )
    return await reserve(login_form, reserve_request, headless=False)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level))

    try:
        result = asyncio.run(_dispatch(args))
    except MissingFieldsError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "missing_fields": exc.missing_fields,
                    "unknown_fields": exc.unknown_fields,
                    "message": "Please provide the missing required fields and try again.",
                },
                ensure_ascii=False,
            )
        )
        return 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    # asyncio.run(debug_reserve_example())
    raise SystemExit(main())
