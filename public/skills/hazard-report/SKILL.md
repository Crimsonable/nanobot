---
name: hazard-report
description: 依据已核实的现场信息生成 HTML、PDF、Word 隐患报告，并通过附件工具交付。
---

先核实照片与引用依据，不能编造隐患、规范条款或检查结论。原项目引用的 citation-rules、workflow 等知识库文件未包含在本仓库，需要部署方放入授权公共目录；缺少依据时明确标注待核实。

1. 在用户工作区 `output/` 写入报告 JSON。字段沿用原脚本：submitter、inspect_time、report_id、image_url_map、scene_keywords、severe_count、general_count、overall_evaluation、severe_hazards、general_hazards、improvement_items、next_steps_reminder。
2. `image_url_map` 的 key 是图片名，value 使用工具可以访问的本地图片路径。用户提供 URL 时，由 agent 按需调用有权限的工具获取并保存到自己的工作目录，再将本地路径交给脚本；CU/runtime 不预下载。脚本不下载 URL，也不读取渠道凭据。
3. 从用户工作目录执行 `python3 /opt/public/skills/hazard-report/scripts/report_generator.py --input-file output/report.json --format pdf`。支持 html、docx、all。输出保存在输入 JSON 所在目录；公共技能目录只读。
4. 读取脚本输出的 files 列表，调用 `publish_attachment`，仅提供 path 和 filename。需要交付源数据时将同名 JSON 一并发布。
5. 工具返回 queued_for_reply 表示已加入本轮回复；最终由 container_up 投递，不能宣称渠道已接收。

脚本仅生成文件，不上传 MinIO，不连接 container_up，不扫描最终正文中的文件路径。需要预览图片时也通过 `publish_attachment` 获得 URL，再引用该 URL。
