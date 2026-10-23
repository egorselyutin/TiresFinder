import streamlit as st
import json
import os
import time
import base64
import re
import subprocess
import sys
from datetime import datetime
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from curl_cffi import requests as cffi_requests

# Автоматическая установка бинарников Playwright для облака
@st.cache_resource
def install_playwright():
    try:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    except Exception:
        pass

install_playwright()

st.set_page_config(
    page_title="Сканер шин — Красноярск",
    page_icon="🛞",
    layout="wide"
)

DB_FILE = "found_tires.json"
COOKIES_FILE = "drom_state.json"

# --- РАБОТА С БАЗОЙ ДАННЫХ ---
def load_data():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_data(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# --- УТИЛИТЫ: ПАРСИНГ И ФИЛЬТРАЦИЯ ---
def parse_price_num(price_str):
    if not price_str:
        return float('inf')
    digits = re.sub(r'[^\d]', '', str(price_str))
    return int(digits) if digits else float('inf')

def extract_year(text):
    """Поиск 4-значного года выпуска в тексте."""
    if not text:
        return None
    match = re.search(r'(?:год\s*выпуска[:\s]*)?\b(201[0-9]|202[0-6])\b', text, re.IGNORECASE)
    return int(match.group(1)) if match else None

def is_new_condition(text):
    """Надежная проверка, указаны ли шины как новые."""
    if not text:
        return False
    text_lower = text.lower()
    
    keywords = [
        "состояние: новое", "состояние новое", "состояние: нов",
        "новое", "новые", "новый", "нов.", "без пробега",
        "новое состояние", "шинный центр", "в наличии", "магазин"
    ]
    
    for kw in keywords:
        if kw in text_lower:
            return True
            
    return False

def is_season_match(text, selected_season):
    """Строгая проверка соответствия выбранной сезонности."""
    if selected_season == "Все":
        return True
    
    text_lower = text.lower()
    winter_keywords = ["зимн", "шип", "winter", "ice", "stud", "frictional", "фрикцион", "липуч", "nordic"]
    summer_keywords = ["летн", "summer"]

    if selected_season == "Летние":
        if any(kw in text_lower for kw in winter_keywords):
            return False
        return True

    elif selected_season == "Зимние":
        if any(kw in text_lower for kw in summer_keywords) and not any(kw in text_lower for kw in winter_keywords):
            return False
        return True

    return True

def parse_avito_details_soup(soup):
    """
    Точечно извлекает год выпуска и состояние из параметров и всего текста Авито.
    """
    year_val = None
    is_new = False

    params_containers = soup.select("[data-marker='item-view/item-params'], [data-marker*='item-params']")
    
    if not params_containers:
        for h2 in soup.find_all(["h2", "h3", "div"]):
            h2_text = h2.get_text(strip=True).lower()
            if "состояние шин" in h2_text or "характеристики" in h2_text:
                parent = h2.find_parent()
                if parent:
                    params_containers.append(parent)
                break

    for container in params_containers:
        for item in container.select("li, div, p, span, tr"):
            text = item.get_text(" ", strip=True)
            text_lower = text.lower()

            if "год выпуска" in text_lower and not year_val:
                match = re.search(r'\b(201[0-9]|202[0-6])\b', text)
                if match:
                    year_val = int(match.group(1))

            if "состояние" in text_lower or "новое" in text_lower:
                if is_new_condition(text):
                    is_new = True

    full_text = soup.get_text(" ", strip=True)
    if not year_val:
        year_val = extract_year(full_text)
    if not is_new:
        is_new = is_new_condition(full_text)

    return year_val, is_new

# --- NTFY ПУШ-УВЕДОМЛЕНИЯ ---
def send_ntfy_push(title, price, link, desc, source, year_val, is_new, topic):
    if not topic.strip():
        return
    try:
        new_tag = "✨ НОВЫЕ! " if is_new else ""
        raw_title = f"🛞 [{source}] {new_tag}{title}"
        b64_title = base64.b64encode(raw_title.encode('utf-8')).decode('ascii')
        encoded_header = f"=?utf-8?B?{b64_title}?="
        
        year_info = f"\n📅 Год: {year_val}" if year_val else "\n📅 Год: Не указан"
        msg = f"📍 Источник: {source}\n💰 Цена: {price}{year_info}\n📝 {desc[:110]}...\n\n🔗 {link}"
        
        cffi_requests.post(
            f"https://ntfy.sh/{topic.strip()}",
            data=msg.encode('utf-8'),
            headers={
                "Title": encoded_header,
                "Click": link,
                "Tags": "car,package"
            },
            timeout=5
        )
    except Exception as e:
        st.error(f"Ошибка ntfy: {e}")

# --- ПАРСИНГ DROM ---
def check_drom_playwright(page, width, profile, radius, season, year_from, enable_year_filter):
    url = f"https://baza.drom.ru/krasnoyarsk/wheel/tire/+/{width}+{profile}+{radius}/?inSetQuantity=4"
    if season == "Летние":
        url += "&sectionSeason[]=summer"
    elif season == "Зимние":
        url += "&sectionSeason[]=winter"
    
    if enable_year_filter and year_from > 0:
        url += f"&yearFrom={year_from}"

    results = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        
        try:
            content = page.content()
        except Exception:
            time.sleep(2)
            content = page.content()

        if "smart-captcha" in content or "Вы не робот?" in content:
            st.warning("⚠️ Дром затребовал капчу! Если вы запущены локально, прокликните её в открывшемся окне (25 сек)...")
            for _ in range(25):
                time.sleep(1)
                try:
                    current_content = page.content()
                    if "smart-captcha" not in current_content and "Вы не робот?" not in current_content:
                        st.success("✅ Капча пройдена!")
                        page.wait_for_load_state("domcontentloaded", timeout=10000)
                        time.sleep(2)
                        break
                except Exception:
                    time.sleep(1)

        soup = BeautifulSoup(page.content(), "html.parser")
        items = soup.select("tr.bull-list-item, div.bull-item, [data-bulletin-id]")

        for item in items:
            title_el = item.select_one("a.bulletinLink, a.bull-item__self-link, .bull-item-content__title a")
            if not title_el:
                continue

            title = title_el.get_text(strip=True)
            href = title_el.get("href", "")
            if not href:
                continue

            link = f"https://baza.drom.ru{href}" if href.startswith("/") else href
            price_el = item.select_one(".price-block__price, .bull-item__price, .price, [data-role='price']")
            price = price_el.get_text(strip=True) if price_el else "Цена не указана"

            desc_el = item.select_one(".bull-item__annotation, .location, .bull-delivery-chunk, .bull-item__attributes")
            desc = desc_el.get_text(strip=True) if desc_el else ""

            bulletin_id = item.get("data-bulletin-id") or href.split("/")[-1].replace(".html", "")
            item_id = f"drom_{bulletin_id}"
            
            raw_item_text = item.get_text(" ", strip=True)
            full_text = f"{title} {desc} {raw_item_text}"

            if not is_season_match(full_text, season):
                continue

            year_val = extract_year(full_text)
            
            if enable_year_filter and year_val and year_val < year_from:
                continue

            is_new = is_new_condition(full_text)

            results.append({
                "id": item_id,
                "source": "Дром",
                "title": title,
                "price": price,
                "price_num": parse_price_num(price),
                "year": year_val,
                "is_new": is_new,
                "link": link,
                "description": desc if desc else ("Новое состояние" if is_new else "Б/У"),
                "found_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
    except Exception as e:
        st.error(f"Ошибка Дрома: {e}")

    return results

# --- ПАРСИНГ AVITO ---
def check_avito_playwright(page, width, profile, radius, season, year_from, enable_year_filter):
    season_str = "летняя" if season == "Летние" else ("зимняя" if season == "Зимние" else "")
    query_str = f"шины {season_str} {width} {profile} R{radius}".strip()
        
    url = f"https://www.avito.ru/krasnoyarsk?q={cffi_requests.utils.quote(query_str)}"
    results = []
    
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        
        try:
            page.wait_for_selector("[data-marker='item']", timeout=10000)
        except Exception:
            pass

        page.mouse.wheel(0, 700)
        time.sleep(1)

        try:
            main_html = page.content()
        except Exception:
            time.sleep(1)
            main_html = page.content()

        soup = BeautifulSoup(main_html, "html.parser")
        items = soup.select("[data-marker='item']")

        for item in items:
            title_el = item.select_one("[data-marker='item-title']")
            price_el = item.select_one("[data-marker='item-price']")
            link_el = item.select_one("a[data-marker='item-title']")

            if not title_el or not link_el:
                continue

            title = title_el.get_text(strip=True)
            price = price_el.get_text(strip=True) if price_el else "Цена не указана"
            href = link_el.get("href", "")
            link = f"https://www.avito.ru{href}" if href.startswith("/") else href

            item_id = f"avito_{href.split('_')[-1]}" if "_" in href else f"avito_{link}"
            
            raw_item_text = item.get_text(" ", strip=True)
            year_val = extract_year(raw_item_text)
            is_new = is_new_condition(raw_item_text)

            # Переход в карточку объявления
            try:
                detail_page = page.context.new_page()
                detail_page.goto(link, wait_until="domcontentloaded", timeout=15000)
                
                try:
                    detail_page.wait_for_selector("[data-marker*='item-params'], h2, h3", timeout=4000)
                except Exception:
                    pass

                time.sleep(1)
                
                try:
                    detail_html = detail_page.content()
                except Exception:
                    time.sleep(1.5)
                    detail_html = detail_page.content()

                detail_soup = BeautifulSoup(detail_html, "html.parser")
                parsed_year, parsed_is_new = parse_avito_details_soup(detail_soup)
                
                if parsed_year:
                    year_val = parsed_year
                if parsed_is_new:
                    is_new = True
                
                detail_page.close()
            except Exception:
                pass

            full_text_for_filter = f"{title} {raw_item_text}"

            if not is_season_match(full_text_for_filter, season):
                continue

            if enable_year_filter and year_val and year_val < year_from:
                continue

            results.append({
                "id": item_id,
                "source": "Авито",
                "title": title,
                "price": price,
                "price_num": parse_price_num(price),
                "year": year_val,
                "is_new": is_new,
                "link": link,
                "description": "Новое состояние" if is_new else "Б/У",
                "found_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
    except Exception as e:
        st.error(f"Ошибка Авито: {e}")

    return results

# --- ИНТЕРФЕЙС STREAMLIT ---
st.title("🛞 Сканер шин (Авито + Дром) — Красноярск")

st.sidebar.header("🎯 Параметры поиска")
col1, col2, col3 = st.sidebar.columns(3)
with col1:
    width = st.number_input("Ширина", value=225, step=5)
with col2:
    profile = st.number_input("Профиль", value=55, step=5)
with col3:
    radius = st.number_input("Радиус (R)", value=19, step=1)

season = st.sidebar.selectbox("Сезонность", ["Все", "Летние", "Зимние"])

st.sidebar.subheader("📅 Настройки года")
enable_year_filter = st.sidebar.checkbox("Учитывать год выпуска", value=False)

current_year = datetime.now().year
default_year = current_year - 1
min_year = current_year - 4

if enable_year_filter:
    year_from = st.sidebar.slider("Год выпуска (от)", min_value=min_year, max_value=current_year, value=default_year, step=1)
else:
    year_from = 0

search_mode = st.sidebar.radio("Режим выдачи", ["Выдавать все найденные", "Только новые (с последней выдачи)"])
search_sources = st.sidebar.multiselect("Площадки", ["Авито", "Дром"], default=["Авито", "Дром"])

user_ntfy_topic = st.sidebar.text_input("Тема ntfy", value="egor_kras_tires_225_55_19")

stored_data = load_data()

if st.button("🔍 Запустить поиск"):
    with st.spinner("Открытие браузера и сканирование..."):
        scraped_items = []
        
        with sync_playwright() as p:
            # Автоматическое определение режима: если запущено в Streamlit Cloud, то headless=True
            is_cloud = os.getenv("STREAMLIT_RUNTIME_ENV") is not None or not os.path.exists("/app") and os.name != "nt"
            # На всякий случай проверяем наличие графической среды (если вы в контейнере без дисплея)
            use_headless = True if os.getenv("CI") or os.getenv("RENDER") or not os.environ.get("DISPLAY") else False
            
            # Для локальной разработки можете принудительно ставить False, но в облаке строго True:
            browser = p.chromium.launch(
                headless=use_headless, 
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage"
                ]
            )
            
            if os.path.exists(COOKIES_FILE):
                try:
                    context = browser.new_context(
                        storage_state=COOKIES_FILE,
                        viewport={"width": 1366, "height": 768},
                        locale="ru-RU"
                    )
                except Exception:
                    context = browser.new_context(
                        viewport={"width": 1366, "height": 768},
                        locale="ru-RU"
                    )
            else:
                context = browser.new_context(
                    viewport={"width": 1366, "height": 768},
                    locale="ru-RU"
                )

            page = context.new_page()

            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            """)

            if "Авито" in search_sources:
                avito_res = check_avito_playwright(page, width, profile, radius, season, year_from, enable_year_filter)
                scraped_items.extend(avito_res)
                st.toast(f"Авито: найдено {len(avito_res)}")
                time.sleep(1)

            if "Дром" in search_sources:
                drom_res = check_drom_playwright(page, width, profile, radius, season, year_from, enable_year_filter)
                scraped_items.extend(drom_res)
                st.toast(f"Дром: найдено {len(drom_res)}")

            try:
                context.storage_state(path=COOKIES_FILE)
            except Exception:
                pass

            browser.close()

        existing_ids = {item["id"] for item in stored_data}
        new_added = 0
        
        if search_mode == "Только новые (с последней выдачи)":
            items_to_process = [item for item in scraped_items if item["id"] not in existing_ids]
        else:
            items_to_process = scraped_items

        for item in items_to_process:
            if item["id"] not in existing_ids:
                stored_data.insert(0, item)
                new_added += 1
                send_ntfy_push(
                    item["title"], 
                    item["price"], 
                    item["link"], 
                    item["description"], 
                    item["source"], 
                    item.get("year"), 
                    item.get("is_new", False),
                    user_ntfy_topic
                )
        
        save_data(stored_data)
        
        if scraped_items:
            st.success(f"Всего найдено: {len(scraped_items)} (новых: {new_added})")
        else:
            st.error("Ни одна из площадок не вернула результатов под заданные фильтры.")
            
        display_list = items_to_process if search_mode == "Только новые (с последней выдачи)" else scraped_items
else:
    display_list = stored_data

# СОРТИРОВКА ПО ЦЕНЕ ПО ВОЗРАСТАНИЮ
display_list = sorted(display_list, key=lambda x: x.get('price_num', parse_price_num(x.get('price', ''))))

st.subheader(f"Результаты ({len(display_list)}) — от дешевых к дорогим 💰")

if display_list:
    if st.button("🗑 Очистить историю"):
        save_data([])
        st.rerun()

    for item in display_list:
        src = item.get('source', 'Объявление')
        year_badge = f" | 📅 {item['year']} г." if item.get('year') else " | 📅 Не указан г."
        new_badge = " ✨ НОВЫЕ!" if item.get('is_new') else ""
        
        with st.expander(f"🟢 [{src}]{new_badge} {item['title']} — {item['price']}{year_badge}"):
            if item.get('is_new'):
                st.success("✨ **Состояние:** Новые!")
            else:
                st.write("**Состояние:** Б/У")
            if item.get('year'):
                st.info(f"📅 **Год выпуска:** {item['year']}")
            else:
                st.warning("📅 **Год выпуска:** Не указан")
            if item.get('description'):
                st.write(f"**Информация:** {item['description']}")
            st.write(f"**Найдено:** {item['found_at']}")
            st.markdown(f"[🔗 Перейти к объявлению]({item['link']})")
else:
    st.write("Список пуст. Нажмите **«Запустить поиск»**.")