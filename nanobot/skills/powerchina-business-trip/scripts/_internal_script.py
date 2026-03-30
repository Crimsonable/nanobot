from dataclasses import MISSING, fields
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, async_playwright


DEFAULT_STATE_PATH = Path.cwd() / "state.json"


class BrowserInitFailed(RuntimeError):
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


class BrowserAgent:
    def __init__(
        self, *, headless: bool = True, state_path: Path | None = DEFAULT_STATE_PATH
    ):
        self.headless = headless
        self.state_path = state_path
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    async def __aenter__(self) -> "BrowserAgent":
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless, args=[]
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
    allow_default_fields: list[str] | None = None,
) -> Any:
    values: dict[str, Any] = {}
    missing: list[str] = []
    valid_fields = {field.name: field for field in fields(model_cls)}
    allow_default_fields = allow_default_fields or []

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


async def _save_state_storage(
    ctx: BrowserContext, state_path: Path = DEFAULT_STATE_PATH
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    await ctx.storage_state(path=str(state_path))
