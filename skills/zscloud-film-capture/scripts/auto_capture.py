# -*- coding: utf-8 -*-
"""zscloud 共享链接全量 DICOM 影像自动抓取脚本。

完整流程:
  1. 打开 https://zscloud.zs-hospital.sh.cn/film/#/shared?code=... 链接
     → 服务器会 302 到 https://zscloud.zs-hospital.sh.cn/film/web/#/thirdParty/share/sharedStudy(报告概览页)
  2. 设置视窗 1920x1080
  3. 在报告页等待并点击「查看影像」,会新开一个 tab 跳转到 viewer
     (URL: /film/web/#/web2d?...&type=sharedStudy)
  4. 切到 viewer tab,等待 viewer iframe + mainview 就绪
  5. 双击选中的协议模板(默认模糊匹配 "5*5")
  6. 切换布局到 1x1
  7. 设置窗宽窗位(WW/WL)
  8. 解析当前协议的总帧数
  9. 逐帧翻页 + canvas toDataURL 抓取 → JPEG 落盘

用法:
  D:/Anaconda/envs/codegen-marker/python.exe \\
      skills/zscloud-film-capture/scripts/auto_capture.py \\
      "https://zscloud.zs-hospital.sh.cn/film/#/shared?code=<CODE>" \\
      --out out/<hospital_name>/canvas_frames

依赖: playwright >= 1.40, 系统已安装 Chromium
"""
import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


DEFAULT_PYTHON = r"D:/Anaconda/envs/codegen-marker/python.exe"

# ═══════════════════════════════════════════════════
# 日志(用 print,Windows GBK 安全字符)
# ═══════════════════════════════════════════════════


def log(msg: str) -> None:
    print(f"[zscloud] {msg}", flush=True)


def die(msg: str, code: int = 1) -> None:
    print(f"[zscloud] FATAL: {msg}", flush=True)
    sys.exit(code)


# ═══════════════════════════════════════════════════
# 协议匹配 + DOM 操作
# ═══════════════════════════════════════════════════

# 协议列表选择器(联影 web2d 通用)
PROTOCOL_CONTAINER_JS = """
() => {
    // 协议面板候选选择器(按优先级)
    const candidates = [
        '.hp-list', '.protocol-list', '.series-protocol-list',
        '[class*="protocol"]', '[class*="Protocol"]',
        '[class*="hanging-protocol"]', '[class*="HangingProtocol"]',
    ];
    for (const sel of candidates) {
        const el = document.querySelector(sel);
        if (el) return sel;
    }
    // 兜底: 找所有 li/div 文本里含 * 数字×数字 的元素
    return null;
}
"""

# 双击协议项(在 iframe 内)
DOUBLE_CLICK_PROTOCOL_JS = """
(needle) => {
    // 协议项可能是 li / div / span
    const all = document.querySelectorAll('li, div, span');
    for (const el of all) {
        const t = (el.innerText || el.textContent || '').trim();
        if (!t) continue;
        if (t.length > 60) continue;  // 协议名一般很短
        if (t === needle) {
            // 检查是否可见
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) {
                // 先滚到视口
                el.scrollIntoView({block: 'center'});
                return {ok: true, text: t, tag: el.tagName, rect: {x: r.x, y: r.y, w: r.width, h: r.height}};
            }
        }
    }
    return {ok: false, reason: 'protocol not found'};
}
"""

# 设置布局(1x1 / 1*1 / 1×1 等)
CHANGE_LAYOUT_JS = """
(layoutNeedle) => {
    const all = document.querySelectorAll('button, div, span, li, a');
    const candidates = [];
    for (const el of all) {
        const t = (el.innerText || el.textContent || '').trim();
        if (!t) continue;
        // 匹配 "1x1", "1*1", "1×1", "1 X 1"
        const m = t.match(/^1\\s*[xX×*]\\s*1$/);
        if (m) {
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) {
                candidates.push({el, text: t, rect: r});
            }
        }
    }
    // 优先选 "按钮样式"(通常有更明显的视觉)
    candidates.sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height));
    if (candidates.length > 0) {
        candidates[0].el.scrollIntoView({block: 'center'});
        candidates[0].el.click();
        return {ok: true, text: candidates[0].text};
    }
    return {ok: false, reason: 'layout button not found'};
}
"""

# 设置 WW/WL — 通常是右下角的两个输入框
# 联影 web2d 的 ww/wl 输入控件常在 viewport-corner / corner-info 区域
SET_WW_WL_JS = """
({ww, wl}) => {
    // 1. 找所有 input[type=number] / input[type=text] / contenteditable
    const inputs = Array.from(document.querySelectorAll('input, [contenteditable="true"]'));

    // 2. 按位置筛:在右下角(viewport 右下 1/3 区域)
    const vw = window.innerWidth, vh = window.innerHeight;
    const rightBottom = inputs.filter(el => {
        const r = el.getBoundingClientRect();
        return r.x > vw * 0.5 && r.y > vh * 0.5 && r.width > 0 && r.width < 100;
    });

    // 3. 找 WW / WL 输入框
    function findByLabel(area, keywords) {
        for (const el of area) {
            // 自身或父节点文本含关键字
            let parent = el;
            for (let i = 0; i < 4 && parent; i++) {
                const txt = (parent.innerText || '').toLowerCase();
                if (keywords.every(k => txt.includes(k.toLowerCase()))) {
                    return el;
                }
                parent = parent.parentElement;
            }
            // placeholder/title
            const ph = ((el.placeholder || '') + ' ' + (el.title || '') + ' ' + (el.getAttribute('aria-label') || '')).toLowerCase();
            if (keywords.every(k => ph.includes(k.toLowerCase()))) {
                return el;
            }
        }
        return null;
    }

    const wwEl = findByLabel(rightBottom, ['ww', 'window width']) ||
                 findByLabel(inputs, ['ww', 'window width']);
    const wlEl = findByLabel(rightBottom, ['wl', 'window level']) ||
                 findByLabel(inputs, ['wl', 'window level']);

    function setVal(el, val) {
        if (!el) return false;
        el.focus();
        if (el.tagName === 'INPUT') {
            el.value = '';
            el.value = String(val);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        } else {
            // contenteditable
            el.innerText = String(val);
            el.dispatchEvent(new Event('input', {bubbles: true}));
        }
        // 触发 blur 或 Enter
        el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
        return true;
    }

    const wwOk = setVal(wwEl, ww);
    const wlOk = setVal(wlEl, wl);
    return {wwOk, wlOk, wwFound: !!wwEl, wlFound: !!wlEl};
}
"""

# 解析总帧数
GET_TOTAL_FRAMES_JS = """
() => {
    try {
        const v = window.mainview.getViewports()[0];
        return v.imageManager.availableImagesIndex.length;
    } catch (e) {
        return null;
    }
}
"""

# 翻页:必须 setCurrFileIndex + pageTurnToCurrFileIndex
TURN_TO_FRAME_JS = """
(idx) => {
    const v = window.mainview.getViewports()[0];
    v.setCurrFileIndex(idx);
    v.pageTurnToCurrFileIndex('manual');
    return true;
}
"""

# 在报告页找「查看影像」或「查看胶片」按钮并返回元素
# 报告页路径: /film/web/#/thirdParty/share/sharedStudy
# viewer 路径: /film/web/#/web2d
# 优先「查看影像」(顶部),兜底「查看胶片」(底部)
#
# 注意: zscloud 报告页上这个按钮是 generic div,不是 A/BUTTON,
#       仅靠 tagName 不可靠。要用 cursor: pointer 作为可点击依据。
FIND_VIEW_BUTTON_JS = """
() => {
    const all = Array.from(document.querySelectorAll('div, span, a, button'));
    const findByText = (needle) => {
        for (const el of all) {
            const t = (el.innerText || el.textContent || '').trim();
            if (t === needle) {
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) {
                    const cs = window.getComputedStyle(el);
                    if (cs.cursor === 'pointer') {
                        return {ok: true, text: t, tag: el.tagName, x: r.x + r.width / 2, y: r.y + r.height / 2};
                    }
                }
            }
        }
        // 兜底: 找不到 cursor:pointer 的精确匹配,找任何含「查看影像」的可见元素
        for (const el of all) {
            const t = (el.innerText || el.textContent || '').trim();
            if (t === needle) {
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) {
                    return {ok: true, text: t, tag: el.tagName, x: r.x + r.width / 2, y: r.y + r.height / 2, fallback: true};
                }
            }
        }
        return null;
    };
    return findByText('查看影像') || findByText('查看胶片') || {ok: false};
}
"""

# 截取当前 canvas(1×1 布局下 id='0_0',但用 querySelectorAll+面积兜底)
CAPTURE_CANVAS_JS = """
() => {
    const target = document.getElementById('0_0') ||
                   document.querySelector('canvas[id$="_0"]') ||
                   (() => {
                       const cs = document.querySelectorAll('canvas');
                       if (!cs.length) return null;
                       let best = cs[0], bestArea = 0;
                       for (const c of cs) {
                           const a = c.width * c.height;
                           if (a > bestArea) { bestArea = a; best = c; }
                       }
                       return best;
                   })();
    if (!target) return null;
    if (target.width === 0 || target.height === 0) return null;  // 未渲染
    return target.toDataURL('image/jpeg', 0.92);
}
"""


# ═══════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════

def run(url: str, out_dir: Path, ww: int, wl: int,
        protocol: str, layout: str, headless: bool, batch_size: int) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    log_file = out_dir.parent / "capture.log" if out_dir.name == "canvas_frames" else out_dir / "capture.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    summary = {
        "url": url, "out": str(out_dir), "ww": ww, "wl": wl,
        "protocol": protocol, "layout": layout,
        "total_frames": 0, "captured": 0, "errors": [],
    }

    log(f"视窗 1920x1080, headless={headless}")
    log(f"导航: {url}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        # 1. 等报告页加载(看 body 是否出现「查看影像」)
        log("等待报告页加载...")
        report_loaded = False
        for i in range(60):  # 最多 60 秒
            try:
                btn = page.evaluate(FIND_VIEW_BUTTON_JS)
                if btn and btn.get("ok"):
                    report_loaded = True
                    break
            except Exception:
                pass
            time.sleep(1)
        if not report_loaded:
            die("报告页 60s 内未加载完(未找到「查看影像」按钮)")

        # 2. 点击「查看影像」,会新开一个 viewer tab
        log("点击「查看影像」,等待 viewer 新 tab...")
        # 重新取一次按钮位置(可能之前 wait 期间 DOM 重渲染了)
        btn = page.evaluate(FIND_VIEW_BUTTON_JS)
        if not btn or not btn.get("ok"):
            die("「查看影像」按钮重新定位失败")

        viewer_page = None
        try:
            with ctx.expect_page(timeout=15000) as new_page_info:
                page.mouse.click(btn["x"], btn["y"])
                log(f"  已点击 '{btn['text']}' ({btn['tag']})")
            viewer_page = new_page_info.value
        except PlaywrightTimeoutError:
            log("  ⚠ 15s 内未出现新 tab,尝试同 tab 跳转兜底...")
            page.mouse.click(btn["x"], btn["y"])
            viewer_page = page
            # 等 URL 或 DOM 出现 viewer 特征
            for _ in range(30):
                try:
                    if viewer_page.locator("iframe").count() > 0:
                        break
                except Exception:
                    pass
                time.sleep(1)

        # 3. 等 viewer tab 的 iframe 加载
        log(f"等待 viewer iframe (URL: {viewer_page.url[:80]}...)...")
        iframe_loc = viewer_page.locator("iframe").first
        try:
            iframe_loc.wait_for(state="attached", timeout=30000)
        except PlaywrightTimeoutError:
            die("iframe 30s 内未出现")

        frame = iframe_loc.content_frame
        if not frame:
            die("无法获取 iframe.content_frame")

        # 等 mainview 就绪
        log("等待 mainview 就绪...")
        for i in range(60):
            try:
                ok = frame.evaluate("() => typeof window.mainview !== 'undefined' && window.mainview && window.mainview.getViewports().length > 0")
                if ok:
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            die("mainview 60s 内未就绪")
        log("viewer 已就绪")
        log(f"viewer URL: {viewer_page.url[:120]}")

        # 2. 双击协议
        log(f"双击协议: {protocol}")
        # 协议项含 * 的格式(如 "5*5"、"5×5"),支持多种写法
        target_texts = list({protocol, protocol.replace("*", "x"), protocol.replace("*", "×")})
        for tgt in target_texts:
            r = frame.evaluate(DOUBLE_CLICK_PROTOCOL_JS, tgt)
            if r and r.get("ok"):
                # 用 Playwright 派发 dblclick(在 viewer tab 上)
                viewer_page.mouse.dblclick(r["rect"]["x"] + r["rect"]["w"] / 2,
                                           r["rect"]["y"] + r["rect"]["h"] / 2)
                log(f"  选中协议: '{r['text']}'")
                break
        else:
            log(f"  ⚠ 协议 '{protocol}' 未找到,尝试模糊匹配")
            # 模糊:把数字提出来,如 "5*5" → /5.5./i
            nums = re.findall(r"\d+", protocol)
            if len(nums) >= 1:
                pattern = r"\d+\s*[*xX×]\s*\d+"
                found = frame.evaluate(
                    "(p) => { const re = new RegExp(p); const all = document.querySelectorAll('li, div, span'); for (const el of all) { const t = (el.innerText||'').trim(); if (re.test(t) && t.length < 20) { const r = el.getBoundingClientRect(); if (r.width>0) return {text:t, x:r.x+r.width/2, y:r.y+r.height/2}; } } return null; }",
                    pattern,
                )
                if found:
                    viewer_page.mouse.dblclick(found["x"], found["y"])
                    log(f"  模糊匹配到: '{found['text']}'")
                else:
                    summary["errors"].append(f"protocol '{protocol}' not found (fuzzy)")

        time.sleep(2.0)  # 让协议渲染

        # 3. 切布局
        log(f"切换布局: {layout}")
        r = frame.evaluate(CHANGE_LAYOUT_JS, layout)
        if r and r.get("ok"):
            log(f"  已切到: '{r['text']}'")
        else:
            log(f"  ⚠ 布局切换失败: {r.get('reason') if r else 'unknown'}")
            summary["errors"].append("layout change failed")
        time.sleep(1.5)

        # 4. 设置 WW/WL
        log(f"设置 WW={ww}, WL={wl}")
        r = frame.evaluate(SET_WW_WL_JS, {"ww": ww, "wl": wl})
        if r:
            log(f"  WW 设置: {r.get('wwOk')} (found={r.get('wwFound')}), "
                f"WL 设置: {r.get('wlOk')} (found={r.get('wlFound')})")
        time.sleep(1.5)

        # 5. 解析总帧数
        log("解析总帧数...")
        total = frame.evaluate(GET_TOTAL_FRAMES_JS)
        if not total or total <= 0:
            die("无法解析总帧数(imageManager 不可用)")
        summary["total_frames"] = total
        log(f"总帧数: {total}")

        # 6. 逐帧抓取
        log(f"开始抓取, batch_size={batch_size}")
        captured = 0
        t0 = time.monotonic()
        idx = 0
        while idx < total:
            batch_end = min(idx + batch_size, total)
            log(f"批次 {idx}..{batch_end - 1}/{total}")
            for i in range(idx, batch_end):
                fstart = time.monotonic()
                if i > 0:
                    frame.evaluate(TURN_TO_FRAME_JS, i)
                    time.sleep(2.8)  # 懒加载 + 解码 + 渲染
                # 截图
                b64 = frame.evaluate(CAPTURE_CANVAS_JS)
                if not b64:
                    log(f"  ⚠ 帧 {i} canvas 为空,重试...")
                    time.sleep(1.5)
                    b64 = frame.evaluate(CAPTURE_CANVAS_JS)
                if not b64:
                    summary["errors"].append(f"frame {i} capture failed")
                    continue
                if b64.startswith("data:"):
                    b64 = b64.split(",", 1)[1]
                fpath = out_dir / f"frame_{i:03d}.jpeg"
                fpath.write_bytes(base64.b64decode(b64))
                captured += 1
                cost = time.monotonic() - fstart
                elapsed = time.monotonic() - t0
                eta = cost * (total - i - 1)
                size_kb = fpath.stat().st_size // 1024
                log(f"  ✓ frame_{i:03d}.jpeg ({size_kb}KB, {cost:.1f}s) "
                    f"[已过 {elapsed:.0f}s, ETA {eta:.0f}s]")
            idx = batch_end

        summary["captured"] = captured
        log(f"完成: {captured}/{total} 帧已保存到 {out_dir}/")

        browser.close()

    # 写 summary
    summary_path = out_dir.parent / "capture_summary.json" if out_dir.name == "canvas_frames" else out_dir / "capture_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Summary: {summary_path}")
    return summary


# ═══════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════

def main() -> None:
    p = argparse.ArgumentParser(
        description="zscloud 共享链接全量 DICOM 影像自动抓取",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("url", help="形如 https://zscloud.zs-hospital.sh.cn/film/#/shared?code=xxxxx")
    p.add_argument("--out", required=True, help="输出目录(会自动创建 canvas_frames/ 子目录)")
    p.add_argument("--ww", type=int, default=2000, help="窗宽 Window Width (默认 2000)")
    p.add_argument("--wl", type=int, default=0, help="窗位 Window Level (默认 0)")
    p.add_argument("--protocol", default="5*5", help="要双击选中的协议名,模糊匹配 (默认 5*5)")
    p.add_argument("--layout", default="1*1", help="切到的最终布局 (默认 1*1)")
    p.add_argument("--batch-size", type=int, default=5, help="单批次帧数 (默认 5, 2.8s/帧)")
    p.add_argument("--headless", action="store_true", help="无头模式(默认有头便于调试)")
    args = p.parse_args()

    out_dir = Path(args.out)
    if out_dir.name != "canvas_frames":
        out_dir = out_dir / "canvas_frames"

    summary = run(
        url=args.url, out_dir=out_dir,
        ww=args.ww, wl=args.wl,
        protocol=args.protocol, layout=args.layout,
        headless=args.headless, batch_size=args.batch_size,
    )
    if summary["errors"]:
        print(f"[zscloud] 完成但有 {len(summary['errors'])} 个错误,见 capture_summary.json", flush=True)


if __name__ == "__main__":
    main()
