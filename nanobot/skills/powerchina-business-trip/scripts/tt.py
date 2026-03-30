import re
from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://cdyyrz.powerchina.cn/idp/authcenter/ActionAuthChain?entityId=apphub")
    page.get_by_text("用户名密码登录").click()
    page.get_by_role("textbox", name="用户名/手机/邮箱").click()
    page.get_by_role("textbox", name="用户名/手机/邮箱").fill("2021088")
    page.get_by_role("textbox", name="用户名/手机/邮箱").press("Tab")
    page.locator("#j_password").fill("jF70wO49")
    page.get_by_role("button", name="登录").click()
    page1 = context.new_page()
    page1.goto("http://192.168.187.54:8080/nccloud/resources/workbench/public/common/main/index.html#/")
    page1.close()
    page2 = context.new_page()
    page2.goto("http://192.168.187.54:8080/nccloud/resources/workbench/public/common/main/index.html#/")
    page2.close()
    page.locator("#spaceLi_-5403360460079280953 > .navName").click()
    with page.expect_popup() as page3_info:
        page.locator("div:nth-child(4) > .group-block > .magnet-type-3 > div > .magnet-name > .d_name").first.click()
    page3 = page3_info.value
    page3.close()
    page4 = context.new_page()
    page4.goto("http://192.168.187.54:8080/nccloud/resources/workbench/public/common/main/index.html#/")
    page4.close()
    page5 = context.new_page()
    page5.goto("http://192.168.187.54:8080/nccloud/resources/workbench/public/common/main/index.html#/")
    page5.locator(".nc-workbench-icon").click()
    page5.get_by_text("个人业务").click()
    with page5.expect_popup() as page6_info:
        page5.get_by_title("出差申请").click()
    page6 = page6_info.value
    page6.locator("#mainiframe").content_frame.get_by_role("button", name="新增").click()
    page6.locator("#mainiframe").content_frame.get_by_role("textbox").nth(2).click()
    page6.locator("#mainiframe").content_frame.get_by_role("textbox").nth(2).fill("本地出差")
    page6.locator("#mainiframe").content_frame.get_by_role("textbox").nth(2).press("Enter")
    page6.locator("#mainiframe").content_frame.get_by_role("textbox").nth(4).fill("2026-03-20 09:00:00")
    page6.locator("#mainiframe").content_frame.get_by_role("textbox").nth(4).press("Enter")
    page6.locator("#mainiframe").content_frame.get_by_role("textbox").nth(5).fill("2026-03-21 09:00:00")
    page6.locator("#mainiframe").content_frame.get_by_role("textbox").nth(5).press("Enter")
    page6.locator("#mainiframe").content_frame.locator(".destination > .form-item-control > .form-component-item-wrapper > .template-item-wrapper > .template-item-wrapper-inner > .base-form-control-wrapper > .wui-input-close > .wui-input").click()
    page6.locator("#mainiframe").content_frame.locator(".destination > .form-item-control > .form-component-item-wrapper > .template-item-wrapper > .template-item-wrapper-inner > .base-form-control-wrapper > .wui-input-close > .wui-input").fill("")
    page6.locator("#mainiframe").content_frame.get_by_role("textbox").nth(5).click()
    page6.locator("#mainiframe").content_frame.get_by_role("textbox").nth(5).click()
    page6.locator("#mainiframe").content_frame.get_by_text("申请人孙宸出差类别本地出差出差开始时间出差结束时间出差时长出差费用交接人目的地出差理由有效期审核人流程类型").click()
    page6.locator("#mainiframe").content_frame.locator(".destination > .form-item-control > .form-component-item-wrapper > .template-item-wrapper > .template-item-wrapper-inner > .base-form-control-wrapper > .wui-input-close > .wui-input").click()
    page6.locator("#mainiframe").content_frame.locator(".destination > .form-item-control > .form-component-item-wrapper > .template-item-wrapper > .template-item-wrapper-inner > .base-form-control-wrapper > .wui-input-close > .wui-input").fill("成都")
    page6.locator("#mainiframe").content_frame.get_by_role("button", name="提交").click()
    page6.locator("#mainiframe").content_frame.get_by_text("撤回").click()
    page6.locator("#mainiframe").content_frame.get_by_role("button", name="确定(Y)").click()

    # ---------------------
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
