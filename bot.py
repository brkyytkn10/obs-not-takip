#!/usr/bin/env python3
"""YTÜ OBS not takip botu.

Modlar:
  discover : login/menü/not sayfalarının HTML + ekran görüntülerini out/discover'a kaydeder.
  run      : giriş yapar, notları çeker, önceki notlarla karşılaştırır, Telegram'a bildirir.

Gizli bilgiler env'den gelir: OBS_USER, OBS_PASS, TG_TOKEN, TG_CHAT_ID. Hiçbiri loglanmaz.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageOps

# --------------------------------------------------------------------------
# Ayarlar. Discover çıktısına bakarak buradaki SELECTORS / NOTES_URL kesinleşecek.
# None = sezgisel (heuristic) bulma kullanılır.
# --------------------------------------------------------------------------
BASE_URL = "https://obs.yildiz.edu.tr/oibs/std/"
LOGIN_URL = BASE_URL
NOTES_URL = None  # örn. BASE_URL + "not_listesi.aspx"; None ise menüden link metniyle bulunur
NOTES_LINK_REGEX = r"not\s*listesi|sınav\s*not|notlar|not\s*bilgi"

SELECTORS = {
    "username": None,
    "password": None,
    "captcha_img": None,
    "captcha_input": None,
    "submit": None,
    "logged_in": None,  # giriş sonrası görünen bir element; None ise "şifre alanı yok" kontrolü
}

# Not tablosundaki bilinen sınav sütunu başlıkları (küçük harf, alt dize eşleşmesi)
EXAM_HEADERS = ["vize", "final", "bütünleme", "butunleme", "ödev", "odev", "quiz",
                "kısa sınav", "kisa sinav", "proje", "lab", "uygulama", "ara sınav", "mazeret"]
COURSE_HEADERS = ["ders adı", "ders adi", "dersin adı", "ders"]

OUT_DIR = Path(os.environ.get("OUT_DIR", "out"))
STATE_DIR = Path(os.environ.get("STATE_DIR", "state"))
GRADES_FILE = STATE_DIR / "grades.json"
STATUS_FILE = STATE_DIR / "status.json"

MAX_LOGIN_ATTEMPTS = 5
FAIL_ALERT_THRESHOLD = 3
NAV_TIMEOUT = 45_000


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
def telegram(text):
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        log("Telegram bilgileri yok, mesaj atlanıyor.")
        return False
    ok = True
    chunks = [text[i:i + 3800] for i in range(0, len(text), 3800)] or [""]
    for chunk in chunks:
        data = urllib.parse.urlencode({"chat_id": chat, "text": chunk}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        try:
            urllib.request.urlopen(req, timeout=30).read()
        except Exception as e:  # URL token içerdiği için exception metnini basmıyoruz
            log(f"Telegram gönderimi başarısız ({type(e).__name__}).")
            ok = False
    return ok


# --------------------------------------------------------------------------
# Captcha
# --------------------------------------------------------------------------
def _otsu(img):
    hist = img.histogram()[:256]
    total = sum(hist)
    sum_all = sum(i * h for i, h in enumerate(hist))
    best_t, best_var, w0, sum0 = 128, -1.0, 0, 0
    for t in range(256):
        w0 += hist[t]
        if w0 == 0:
            continue
        w1 = total - w0
        if w1 == 0:
            break
        sum0 += t * hist[t]
        m0, m1 = sum0 / w0, (sum_all - sum0) / w1
        var = w0 * w1 * (m0 - m1) ** 2
        if var > best_var:
            best_var, best_t = var, t
    return best_t


def preprocess_captcha(src: Path, dst: Path):
    """Gri ton, 3x büyütme, threshold (Otsu). Metin koyu, zemin beyaz olacak şekilde."""
    img = Image.open(src).convert("RGBA")
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    img = Image.alpha_composite(bg, img).convert("L")
    img = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)
    t = _otsu(img)
    img = img.point(lambda p: 255 if p > t else 0)
    if sum(img.histogram()[128:]) < sum(img.histogram()[:128]):  # çoğunluk siyahsa ters çevir
        img = ImageOps.invert(img)
    img = ImageOps.expand(img, border=20, fill=255)
    img.save(dst)


def ocr_captcha(img_path: Path):
    """(ham_metin, toplam|None) döndürür."""
    r = subprocess.run(
        ["tesseract", str(img_path), "stdout", "--psm", "7",
         "-c", "tessedit_char_whitelist=0123456789+"],
        capture_output=True, text=True, timeout=30)
    raw = r.stdout.strip()
    m = re.search(r"(\d+)\s*\+\s*(\d+)", raw)
    return raw, (int(m.group(1)) + int(m.group(2)) if m else None)


# --------------------------------------------------------------------------
# Sayfa yardımcıları
# --------------------------------------------------------------------------
def save_page(page, name, folder):
    folder.mkdir(parents=True, exist_ok=True)
    try:
        (folder / f"{name}.html").write_text(page.content(), encoding="utf-8")
    except Exception as e:
        log(f"{name}: html alınamadı ({type(e).__name__})")
    try:
        page.screenshot(path=str(folder / f"{name}.png"), full_page=True)
    except Exception as e:
        log(f"{name}: screenshot alınamadı ({type(e).__name__})")
    # iframe'ler (eski OBS sayfaları sık kullanır)
    for i, fr in enumerate(page.frames[1:], 1):
        try:
            (folder / f"{name}_frame{i}.html").write_text(
                f"<!-- frame url: {fr.url} -->\n" + fr.content(), encoding="utf-8")
        except Exception:
            pass


def _loc(page, key, fallback):
    return page.locator(SELECTORS[key] or fallback).first


def find_login_fields(page):
    pw = _loc(page, "password", "input[type=password]:visible")
    if SELECTORS["username"] and SELECTORS["captcha_input"]:
        return _loc(page, "username", ""), pw, _loc(page, "captcha_input", "")
    inputs = page.locator(
        "input:visible:not([type=hidden]):not([type=password]):not([type=submit])"
        ":not([type=button]):not([type=checkbox]):not([type=image])")
    n = inputs.count()
    if n == 0:
        raise RuntimeError("Login sayfasında text input bulunamadı")
    user = _loc(page, "username", "") if SELECTORS["username"] else inputs.nth(0)
    cap = None
    for i in range(n):
        attrs = " ".join(filter(None, [inputs.nth(i).get_attribute("id"),
                                       inputs.nth(i).get_attribute("name")]))
        if re.search(r"captcha|guvenlik|güvenlik|kod|code|security|image", attrs, re.I):
            cap = inputs.nth(i)
            break
    if SELECTORS["captcha_input"]:
        cap = _loc(page, "captcha_input", "")
    if cap is None:
        cap = inputs.nth(n - 1)
    return user, pw, cap


def find_captcha_img(page):
    if SELECTORS["captcha_img"]:
        return page.locator(SELECTORS["captcha_img"]).first
    imgs = page.locator("img:visible")
    best = None
    for i in range(imgs.count()):
        el = imgs.nth(i)
        desc = " ".join(filter(None, [el.get_attribute("src"), el.get_attribute("id"),
                                      el.get_attribute("alt"), el.get_attribute("class")]))
        if re.search(r"captcha|guvenlik|güvenlik|security|kod|code|image\.aspx|verify", desc, re.I):
            return el
        box = el.bounding_box()
        if box and box["width"] < 300 and box["height"] < 120 and best is None:
            best = el
    if best is None:
        raise RuntimeError("Captcha resmi bulunamadı")
    return best


def login_form_present(page):
    return page.locator(SELECTORS["password"] or "input[type=password]:visible").count() > 0


def is_logged_in(page):
    if SELECTORS["logged_in"]:
        return page.locator(SELECTORS["logged_in"]).count() > 0
    return not login_form_present(page)


def settle(page):
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass


def login(page, discover_dir=None):
    """Captcha çözerek giriş yapar. discover_dir verilirse her denemenin captcha'sını kaydeder."""
    user, pw_ = os.environ["OBS_USER"], os.environ["OBS_PASS"]
    for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
        page.goto(LOGIN_URL, timeout=NAV_TIMEOUT)  # her denemede sayfayı yenile
        settle(page)
        if is_logged_in(page) and not login_form_present(page):
            log("Zaten giriş yapılmış görünüyor.")
            return True
        try:
            u_el, p_el, c_el = find_login_fields(page)
            img_el = find_captcha_img(page)
            work = (discover_dir or OUT_DIR / "tmp")
            work.mkdir(parents=True, exist_ok=True)
            raw_png = work / f"captcha_{attempt}.png"
            proc_png = work / f"captcha_{attempt}_processed.png"
            img_el.screenshot(path=str(raw_png))
            preprocess_captcha(raw_png, proc_png)
            raw, total = ocr_captcha(proc_png)
        except Exception as e:
            log(f"Deneme {attempt}: login alanları/captcha hatası: {type(e).__name__}: {e}")
            continue
        log(f"Deneme {attempt}: OCR='{raw}' toplam={total}")
        if discover_dir:
            (discover_dir / f"captcha_{attempt}.txt").write_text(f"ocr={raw!r} sum={total}\n")
        else:
            raw_png.unlink(missing_ok=True)
            proc_png.unlink(missing_ok=True)
        if total is None:
            continue
        u_el.fill(user)
        p_el.fill(pw_)
        c_el.fill(str(total))
        if SELECTORS["submit"] or page.locator("button[type=submit], input[type=submit]").count():
            _loc(page, "submit", "button[type=submit], input[type=submit]").click()
        else:
            c_el.press("Enter")
        settle(page)
        if is_logged_in(page):
            log(f"Giriş başarılı (deneme {attempt}).")
            return True
        log(f"Deneme {attempt}: giriş başarısız.")
    return False


def open_notes(page):
    """Not sayfasına gider. Başarısızsa RuntimeError."""
    if NOTES_URL:
        page.goto(NOTES_URL, timeout=NAV_TIMEOUT)
        settle(page)
        return
    link = page.get_by_role("link", name=re.compile(NOTES_LINK_REGEX, re.I)).first
    if link.count() == 0:
        raise RuntimeError("Not sayfası linki bulunamadı; NOTES_URL ayarlanmalı")
    link.click()
    settle(page)


# --------------------------------------------------------------------------
# Not tablosu -> {ders: {sınav: not}}
# --------------------------------------------------------------------------
TABLES_JS = """() => Array.from(document.querySelectorAll('table')).map(t =>
  Array.from(t.rows).map(r => Array.from(r.cells).map(c => c.innerText.trim().replace(/\\s+/g,' '))))"""


def parse_tables(tables):
    """Geniş tablo: sütun başlıkları sınav türleri. Satır başına ders.
    NOT: gerçek yapı discover çıktısından sonra kesinleştirilecek."""
    result = {}
    for rows in tables:
        header_idx = None
        for i, row in enumerate(rows[:5]):
            low = [c.lower() for c in row]
            if any(h in c for c in low for h in COURSE_HEADERS) and \
               any(h in c for c in low for h in EXAM_HEADERS):
                header_idx = i
                break
        if header_idx is None:
            continue
        header = [c.lower() for c in rows[header_idx]]
        course_col = next((j for j, c in enumerate(header)
                           if any(h in c for h in COURSE_HEADERS)), 0)
        exam_cols = {j: rows[header_idx][j] for j, c in enumerate(header)
                     if any(h in c for h in EXAM_HEADERS)}
        for row in rows[header_idx + 1:]:
            if len(row) <= course_col or not row[course_col]:
                continue
            course = row[course_col]
            for j, exam in exam_cols.items():
                if j < len(row):
                    val = row[j].strip()
                    if val and val not in ("-", "—", "–", "NA", "N/A"):
                        result.setdefault(course, {})[exam] = val
    return result


def scrape_grades(page):
    tables = []
    for fr in page.frames:
        try:
            tables += fr.evaluate(TABLES_JS)
        except Exception:
            pass
    grades = parse_tables(tables)
    if not grades:
        raise RuntimeError("Not tablosu ayrıştırılamadı (tablo bulunamadı ya da boş)")
    return grades


def diff_grades(old, new):
    msgs = []
    for course, exams in new.items():
        for exam, val in exams.items():
            prev = old.get(course, {}).get(exam)
            if prev is None:
                msgs.append(f"📢 {course} – {exam}: {val}")
            elif prev != val:
                msgs.append(f"📢 {course} – {exam}: {val} (önceki: {prev})")
    return msgs


def summary(grades):
    lines = ["📋 Mevcut notlar:"]
    for course, exams in grades.items():
        lines.append(f"• {course}: " + ", ".join(f"{e} {v}" for e, v in exams.items()))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Durum (cache içinde tutulur, repoya commit edilmez)
# --------------------------------------------------------------------------
def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def record_failure(reason):
    st = load_json(STATUS_FILE, {"fails": 0, "alerted": False})
    st["fails"] += 1
    log(f"Başarısız çalıştırma #{st['fails']} üst üste: {reason}")
    if st["fails"] >= FAIL_ALERT_THRESHOLD and not st["alerted"]:
        if telegram(f"⚠️ OBS botu {st['fails']} çalıştırmadır üst üste başarısız: {reason}"):
            st["alerted"] = True
    save_json(STATUS_FILE, st)


def record_success():
    st = load_json(STATUS_FILE, {"fails": 0, "alerted": False})
    if st.get("alerted"):
        telegram("✅ OBS botu tekrar çalışıyor.")
    save_json(STATUS_FILE, {"fails": 0, "alerted": False})


# --------------------------------------------------------------------------
# Modlar
# --------------------------------------------------------------------------
def launch(p):
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1366, "height": 900}, locale="tr-TR")
    ctx.set_default_timeout(20_000)
    return browser, ctx.new_page()


def mode_discover():
    from playwright.sync_api import sync_playwright
    d = OUT_DIR / "discover"
    d.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser, page = launch(p)
        try:
            page.goto(LOGIN_URL, timeout=NAV_TIMEOUT)
            settle(page)
            log(f"Login sayfası URL: {page.url}")
            save_page(page, "01_login", d)
            summ = {
                "url": page.url,
                "inputs": page.eval_on_selector_all(
                    "input,button,select",
                    "els => els.map(e => ({tag:e.tagName,type:e.type,id:e.id,name:e.name,cls:e.className,value:e.type==='password'?'':e.value}))"),
                "images": page.eval_on_selector_all(
                    "img", "els => els.map(e => ({src:e.src,id:e.id,alt:e.alt,w:e.width,h:e.height}))"),
                "frames": [f.url for f in page.frames],
            }
            save_json(d / "01_login_summary.json", summ)
            try:
                find_captcha_img(page).screenshot(path=str(d / "captcha_page_load.png"))
            except Exception as e:
                log(f"Captcha resmi alınamadı: {e}")

            ok = False
            if os.environ.get("OBS_USER") and os.environ.get("OBS_PASS"):
                ok = login(page, discover_dir=d)
            else:
                log("OBS_USER/OBS_PASS yok; sadece login sayfası kaydedildi.")
            log(f"Giriş sonucu: {ok}")
            if not ok:
                save_page(page, "02_after_failed_login", d)
                return
            save_page(page, "02_menu", d)
            links = page.eval_on_selector_all(
                "a", "els => els.map(e => ({text:e.innerText.trim(),href:e.getAttribute('href'),id:e.id,target:e.target}))")
            save_json(d / "02_links.json", links)
            cands = [l for l in links if l["text"] and re.search(NOTES_LINK_REGEX, l["text"], re.I)]
            save_json(d / "02_notes_link_candidates.json", cands)
            for i, l in enumerate(cands[:4], 1):
                try:
                    page.get_by_role("link", name=l["text"]).first.click()
                    settle(page)
                    save_page(page, f"03_notes_{i}", d)
                    log(f"Not sayfası adayı {i}: {l['text']!r} -> {page.url}")
                    page.go_back()
                    settle(page)
                except Exception as e:
                    log(f"Aday {i} açılamadı: {type(e).__name__}")
        finally:
            browser.close()


def mode_run():
    from playwright.sync_api import sync_playwright
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser, page = launch(p)
        try:
            if not login(page):
                save_page(page, "run_login_failed", OUT_DIR / "error")
                record_failure("giriş yapılamadı (captcha/kimlik bilgisi)")
                return 1
            open_notes(page)
            try:
                grades = scrape_grades(page)
            except Exception:
                save_page(page, "run_notes_parse_failed", OUT_DIR / "error")
                raise
        except Exception as e:
            try:
                save_page(page, "run_error", OUT_DIR / "error")
            except Exception:
                pass
            record_failure(f"{type(e).__name__}: {e}")
            return 1
        finally:
            browser.close()

    old = load_json(GRADES_FILE, None)
    if old is None:
        telegram(summary(grades))
    else:
        msgs = diff_grades(old, grades)
        if msgs:
            telegram("\n".join(msgs))
        log(f"{len(msgs)} yeni/değişen not.")
    save_json(GRADES_FILE, grades)
    record_success()
    return 0


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MODE", "run")).strip()
    if mode == "discover":
        mode_discover()
        return 0
    if mode == "run":
        return mode_run()
    log(f"Bilinmeyen mod: {mode}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
