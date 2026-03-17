---
name: powerchina-meeting-room
description: 当任务涉及 PowerChina 登录、复用 `state.json`、查询空闲会议室、或自动提交会议室预定时使用此 skill。适用于本工作区中的会议室自动化脚本维护、调用和调试。
---

# Powerchina Meeting Room

## 概述

这个 skill 用于处理 PowerChina 登录、会议室空闲查询和完整预定流程。
当任务需要保存登录状态、复用缓存状态、查询空闲会议室，或自动提交会议室预定时，应使用这个 skill。

## 工作流

1. 准备登录、查询或预定所需的 JSON 数据。
2. 执行 `scripts/meeting_room.py`。
3. 从标准输出读取 JSON 结果，作为最终结果。
4. 如果 `get-idle` 或 `reserve` 所需字段不完整，不要猜测，明确告诉用户缺少哪些字段，并请用户补全后再执行。

## 命令

```powershell
python skills/powerchina-meeting-room/scripts/meeting_room.py login --login-file login.json
python skills/powerchina-meeting-room/scripts/meeting_room.py get-idle --meeting-file meeting.json
python skills/powerchina-meeting-room/scripts/meeting_room.py reserve --login-file login.json --meeting-file reserve.json
```

可选参数：

- `--state-path <path>`：指定或复用 Playwright 登录状态文件
- `--headless`：启用无头模式；本地调试时通常不要加
- `--log-level DEBUG|INFO|WARNING|ERROR|CRITICAL`：设置日志级别

登录行为说明：

- `login` 和 `reserve` 中的登录步骤会强制使用全新浏览器上下文，不读取已有缓存状态。
- 登录成功后仍会把新的状态写回 `state.json`，供后续 `get-idle` 复用。

## 枚举映射

以下字段不再直接传中文，而是传布尔值或索引，由脚本统一映射到页面选项。

布尔值字段：

- `leader_flag`
  - `true` -> `有`
  - `false` -> `无`
- `reservation.allow_phone`
  - `true` -> `是`
  - `false` -> `否`

索引字段：

- `office_area`
  - `0` -> `成都`
  - `1` -> `温江`
  - `2` -> `研发中心`
- `slot`
  - `0` -> `上午`
  - `1` -> `下午`
  - `2` -> `全天(9:00-17:00)`
  - `3` -> `晚上`

## 数据格式

登录 JSON：

```json
{
  "usrname": "your-account",
  "pwd": "your-password"
}
```

`login` 必填字段：

- `usrname`
- `pwd`

查询空闲会议室 JSON：

```json
{
  "office_area": 0,
  "date": "2026-03-12",
  "slot": 0,
  "start_time": "2026-03-12 09:00",
  "end_time": "2026-03-12 10:00"
}
```

`get-idle` 必填字段：

- `office_area`
- `date`
- `slot`
- `start_time`
- `end_time`

如果这些字段缺失或为空，必须直接告诉用户缺少哪些字段，并要求用户补全。

预定 JSON：

```json
{
  "meeting": {
    "phone": "18200000000",
    "meeting_name": "project-sync",
    "office_area": 0,
    "date": "2026-03-12",
    "slot": 0,
    "start_time": "2026-03-12 09:00",
    "end_time": "2026-03-12 10:00",
    "headcount": "12",
    "leader_flag": false
  },
  "reservation": {
    "selected_room": "Room A",
    "allow_phone": false
  }
}
```

`reserve` 必填字段：

- 登录信息：`usrname`、`pwd`
- 会议信息：`meeting.phone`、`meeting.meeting_name`、`meeting.office_area`、`meeting.date`、`meeting.slot`、`meeting.start_time`、`meeting.end_time`、`meeting.headcount`、`meeting.leader_flag`

`reserve` 可选字段：

- `reservation.selected_room`
- `reservation.allow_phone`

## 预定规则

- 脚本会同时校验登录信息和预定信息，并一次性返回全部缺失字段。
- 缺失字段会带前缀返回，例如 `login.usrname`、`meeting.start_time`。
- 如果未提供 `reservation.selected_room`，脚本会默认选择系统返回的第一间空闲会议室。
- `meeting.leader_flag` 和 `reservation.allow_phone` 必须传布尔值。
- `meeting.office_area`、`meeting.slot` 和 `get-idle` 里的同名字段必须传索引值。
- 如果索引越界，脚本会直接报错，不会继续执行页面操作。
- 预定流程固定为：登录 -> 打开表单 -> 填写会议信息 -> 查询空闲会议室 -> 选择会议室 -> 填写带手机标志 -> 提交

## 说明

- 完整实现位于 `scripts/meeting_room.py`。
- 会议表单字段填充依赖根目录下的 `operate.js`。
- 带手机标志依赖根目录下的 `operate_phone.js`。
- 如果目标页面字段或选择器发生变化，优先更新对应的 JS 脚本，而不是在 Python 中硬编码页面操作。
