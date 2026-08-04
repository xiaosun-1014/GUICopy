"""诊断 headless 模式下报告页 DOM 结构与「查看影像」按钮状态。"""
from playwright.sync_api import sync_playwright
import time, json, sys

URL = sys.argv[1] if len(sys.argv) > 1 else "https://zscloud.zs-hospital.sh.cn/film/#/shared?code=xg06q2"

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1920, "height": 1080})
    page = ctx.new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)

    # 轮询,每 5 秒打一次「查看影像」候选数 + URL + 页面文本快照
    for i in range(12):
        time.sleep(5)
        try:
            snap = page.evaluate("""() => {
                const all = Array.from(document.querySelectorAll('div, span, a, button'));
                const cands = [];
                for (const el of all) {
                    const t = (el.innerText || el.textContent || '').trim();
                    if (t.includes('查看影像') || t.includes('查看胶片')) {
                        const r = el.getBoundingClientRect();
                        const cs = window.getComputedStyle(el);
                        cands.push({
                            tag: el.tagName,
                            text: t.slice(0, 30),
                            len: t.length,
                            visible: r.width > 0 && r.height > 0,
                            cursor: cs.cursor,
                            x: Math.round(r.x),
                            y: Math.round(r.y),
                            w: Math.round(r.width),
                            h: Math.round(r.height),
                        });
                    }
                }
                return {
                    url: location.href,
                    title: document.title,
                    bodyChars: document.body.innerText.length,
                    bodySnippet: document.body.innerText.slice(0, 200),
                    cands: cands.slice(0, 10),
                };
            }""")
            print(f"\n=== t={i*5+5}s ===")
            print(f"URL:   {snap['url']}")
            print(f"title: {snap['title']}")
            print(f"body len: {snap['bodyChars']}")
            print(f"body snippet: {repr(snap['bodySnippet'])}")
            print(f"cands ({len(snap['cands'])}):")
            for c in snap['cands']:
                print(f"  {c}")
            if snap['cands']:
                break
        except Exception as e:
            print(f"t={i*5+5}s error: {e}")
    browser.close()
