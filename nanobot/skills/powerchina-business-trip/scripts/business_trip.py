from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from datetime import datetime, timedelta

from playwright.async_api import Page

from _internal_script import (
    DEFAULT_STATE_PATH,
    BrowserAgent,
    BrowserInitFailed,
    MissingFieldsError,
    _dataclass_from_payload,
    _merge_missing_errors,
    _prefix_error,
    _save_state_storage,
)


LOGIN_URL = "https://cdyyrz.powerchina.cn/"
HR_URL = "http://192.168.187.54:8080/nccloud/resources/workbench/public/common/main/index.html#/"
TRIP_TYPE_OPTIONS = [
    "省内公务出差",
    "外地出差",
    "国外公务出差",
    "本地出差",
    "省外（发达）公务出差",
    "国外工地出差",
    "国内工地出差",
    "省外（一般）公务出差",
]

logger = logging.getLogger(__name__)


@dataclass
class LoginForm:
    usrname: str
    pwd: str


@dataclass
class BusinessTripRequest:
    trip_type: str
    start_time: str
    end_time: str
    destination: str
    trip_reason: str = ""


async def _fill_login_form_by_locator(page: Page, form: dict[str, Any]) -> None:
    await page.goto(LOGIN_URL)
    await page.set_viewport_size({"width": 1280, "height": 900})
    await page.wait_for_load_state("domcontentloaded")

    if urlparse(page.url).hostname != urlparse(LOGIN_URL).hostname:
        logger.info("Login state is already valid.")
        return

    await page.get_by_text("用户名密码登录", exact=True).click()
    await page.locator("#j_username").fill(form["usrname"])
    await page.locator("#j_password").fill(form["pwd"])
    await page.get_by_role("button", name="登录").click()
    await page.wait_for_load_state("domcontentloaded")


def _require_trip_type(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("trip.trip_type must be a string.")
    if value not in TRIP_TYPE_OPTIONS:
        raise ValueError(
            "trip.trip_type must be one of: " + ", ".join(TRIP_TYPE_OPTIONS)
        )
    return value


def _normalize_trip_request(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "trip_type": _require_trip_type(payload["trip_type"]),
        "start_time": payload["start_time"],
        "end_time": payload["end_time"],
        "destination": payload["destination"],
    }


async def login(
    login_info: dict[str, Any],
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    headless: bool = True,
) -> dict[str, Any]:
    validated_login = _dataclass_from_payload(LoginForm, login_info)

    async with BrowserAgent(headless=headless, state_path=None) as browser:
        if not browser.page or not browser.context:
            raise BrowserInitFailed("Playwright browser not initialized.")
        await _fill_login_form_by_locator(browser.page, validated_login.__dict__)
        await _save_state_storage(browser.context, state_path=state_path)

    return {"ok": True, "state_path": str(state_path)}


async def apply_trip(
    login_info: dict[str, Any],
    trip_request: dict[str, Any],
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    headless: bool = True,
) -> dict[str, Any]:
    errors: list[MissingFieldsError] = []

    try:
        validated_login = _dataclass_from_payload(LoginForm, login_info)
    except MissingFieldsError as exc:
        errors.append(_prefix_error(exc, "login"))

    try:
        validated_trip = _dataclass_from_payload(
            BusinessTripRequest,
            trip_request,
            require_all_non_default=False,
            allow_default_fields=["trip_reason"],
        )
    except MissingFieldsError as exc:
        errors.append(_prefix_error(exc, "trip"))

    if errors:
        raise _merge_missing_errors(*errors)

    start_time = (
        datetime.strptime(trip_request["start_time"], "%Y-%m-%d %H:%M:%S")
        - timedelta(hours=8)
    ).strftime("%Y-%m-%d %H:%M:%S")
    end_time = (
        datetime.strptime(trip_request["end_time"], "%Y-%m-%d %H:%M:%S")
        - timedelta(hours=8)
    ).strftime("%Y-%m-%d %H:%M:%S")

    async with BrowserAgent(headless=headless, state_path=None) as browser:
        if not browser.page or not browser.context:
            raise BrowserInitFailed("Playwright browser not initialized.")

        context = await browser.browser.new_context(
            viewport={"width": 1280, "height": 900}
        )
        page = await context.new_page()

        await _fill_login_form_by_locator(page, validated_login.__dict__)
        await _save_state_storage(browser.context, state_path=state_path)
        await page.locator("#spaceLi_-5403360460079280953 > .navName").click()
        await page.wait_for_load_state("domcontentloaded")
        async with page.expect_popup() as popup:
            await page.locator(".vportal.vp-human-resources").click()
        popup_page = await popup.value
        await popup_page.wait_for_load_state("domcontentloaded")
        await popup_page.locator("i").first.click()
        await popup_page.get_by_text("个人业务").click()
        async with popup_page.expect_popup() as popup:
            await popup_page.get_by_title("出差申请").click()
        await popup_page.close()
        popup_page = await popup.value
        await popup_page.locator("#mainiframe").content_frame.get_by_role(
            "button", name="新增"
        ).click()

        await popup_page.locator("#mainiframe").content_frame.locator(
            "div.triptypeid div > input"
        ).click()
        await popup_page.locator("#mainiframe").content_frame.locator(
            "div.triptypeid div > input"
        ).fill(trip_request["trip_type"])
        await popup_page.wait_for_timeout(1000)
        await popup_page.locator("#mainiframe").content_frame.locator(
            "div.triptypeid div > input"
        ).press("Enter")

        await popup_page.locator("#mainiframe").content_frame.locator(
            "div.tripbegintime input"
        ).click()
        await popup_page.locator("#mainiframe").content_frame.locator(
            "div.tripbegintime input"
        ).type(start_time, delay=300)
        await popup_page.wait_for_timeout(1000)
        await popup_page.locator("#mainiframe").content_frame.locator(
            "div.tripbegintime input"
        ).press("Enter")

        await popup_page.locator("#mainiframe").content_frame.locator(
            "div.tripendtime input"
        ).click()
        await popup_page.locator("#mainiframe").content_frame.locator(
            "div.tripendtime input"
        ).type(end_time, delay=300)
        await popup_page.wait_for_timeout(1000)
        await popup_page.locator("#mainiframe").content_frame.locator(
            "div.tripendtime input"
        ).press("Enter")

        await popup_page.locator("#mainiframe").content_frame.locator(
            "div.destination input"
        ).click()
        await popup_page.locator("#mainiframe").content_frame.locator(
            "div.destination input"
        ).fill(trip_request["destination"])
        await popup_page.locator("#mainiframe").content_frame.locator(
            "div.destination input"
        ).press("Enter")

        if trip_request.get("trip_reason", None):
            await popup_page.locator("#mainiframe").content_frame.locator(
                "div.remark input"
            ).click()
            await popup_page.locator("#mainiframe").content_frame.locator(
                "div.remark input"
            ).fill(trip_request["trip_reason"])
            await popup_page.locator("#mainiframe").content_frame.locator(
                "div.remark input"
            ).press("Enter")

        await popup_page.locator("#mainiframe").content_frame.get_by_text(
            "提交"
        ).click()
        await popup_page.wait_for_timeout(5000)
    tmp_res = {
        "ok": True,
        "message": "您申请的{}~{}{}已提交".format(
            trip_request["start_time"],
            trip_request["end_time"],
            trip_request["trip_type"],
        ),
        "trip_request": _normalize_trip_request(validated_trip.__dict__),
    }
    print(tmp_res)
    return tmp_res


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="登录 PowerChina 或处理出差申请骨架流程。"
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

    """login_parser = subparsers.add_parser("login", help="只登录并保存状态。")
    login_parser.add_argument(
        "--login-json", help='Inline login JSON, e.g. \'{"usrname":"u","pwd":"p"}\'.'
    )"""

    apply_parser = subparsers.add_parser(
        "apply", help="校验出差申请参数，执行登录，并返回骨架状态。"
    )
    apply_parser.add_argument(
        "--login-json", help='Inline login JSON, e.g. \'{"usrname":"u","pwd":"p"}\'.'
    )
    apply_parser.add_argument(
        "--trip-json",
        help='Inline trip JSON. Shape: {"trip_type":"省内公务出差","start_time":"2026-03-18 09:00","end_time":"2026-03-20 18:00","destination":"北京"}.',
    )

    return parser


async def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    state_path = Path(args.state_path).expanduser().resolve()

    if args.command == "login":
        return await login(
            json.loads(args.login_json or "{}"),
            state_path=state_path,
            headless=args.headless,
        )

    if args.command == "apply":
        return await apply_trip(
            json.loads(args.login_json or "{}"),
            json.loads(args.trip_json or "{}"),
            state_path=state_path,
            headless=args.headless,
        )

    raise ValueError(f"Unsupported command: {args.command}")


async def debug_apply_trip_example() -> dict[str, Any]:
    login_form = {
        "usrname": "2021088",
        "pwd": "jF70wO49",
    }
    trip_request = {
        "trip_type": "省内公务出差",
        "start_time": "2026-03-24 09:00:00",
        "end_time": "2026-03-24 18:00:00",
        "destination": "测试",
    }
    return await apply_trip(
        login_info=login_form,
        trip_request=trip_request,
        headless=True,
    )


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
                    "message": "请补齐必填字段后重试。",
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
    raise SystemExit(main())
    # asyncio.run(debug_apply_trip_example())
