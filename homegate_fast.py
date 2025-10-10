# -*- coding: utf-8 -*-
"""
Homegate Rent & Buy Data + Images Scraper - OPTIMIZED VERSION
Major improvements:
- Parallel image downloads with ThreadPoolExecutor
- Reduced sleep times
- Connection pooling for HTTP requests
- Optimized WebDriver waits
- Smart caching to avoid duplicate scraping
"""

import os
import time
import re
import hashlib
import requests
import logging
import pandas as pd
import undetected_chromedriver as uc
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ---------- CONFIGURATION ----------
MAX_IMAGE_WORKERS = 10  # Parallel image downloads
REDUCED_WAIT_TIME = 5   # Reduced from 15 seconds
PAGE_LOAD_WAIT = 2      # Reduced from 2-3 seconds
PROPERTY_LOAD_WAIT = 2  # Reduced from 3 seconds

# Session for connection pooling
session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
})

# Lock to guard shared in-memory structures during parallel writes
download_hashes_lock = threading.Lock()

# ---------- DRIVER SETUP ----------
def init_driver():
    options = uc.ChromeOptions()
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--disable-extensions')
    options.add_argument('--disable-plugins')
    options.add_argument('--headless')
    options.add_argument('--window-size=1920,1080')
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument('--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
    # Performance optimizations
    options.add_argument('--disable-dev-tools')
    options.add_argument('--disable-logging')
    options.add_argument('--log-level=3')
    options.add_argument(f"--remote-debugging-port={8800}")

    options.page_load_strategy = 'eager'  # Don't wait for all resources

    try:
        driver = uc.Chrome(options=options)
    except:
        from selenium import webdriver
        driver = webdriver.Chrome(options=options)
    
    wait = WebDriverWait(driver, REDUCED_WAIT_TIME)
    return driver, wait

# ---------- SAFE FIND ----------
def safe_find(driver, by, value, attr=None, default="not found"):
    try:
        el = driver.find_element(by, value)
        return el.get_attribute(attr) if attr else el.text.strip()
    except:
        return default

# ---------- LAT/LONG ----------
def extract_coordinates_from_iframe(driver, wait):
    try:
        iframe = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "iframe[src*='google.com/maps']")))
        src_url = iframe.get_attribute("src")
        if src_url:
            return parse_coordinates_from_url(src_url)
    except TimeoutException:
        pass
    return None, None

def parse_coordinates_from_url(url):
    pattern = r"q=(-?\d+\.?\d*),(-?\d+\.?\d*)"
    match = re.search(pattern, url)
    if match:
        lat, lng = match.groups()
        return float(lat), float(lng)
    return None, None

# Added: Retry, Exceptions. removed hashlib
# ---------- OPTIMIZED IMAGE DOWNLOAD ----------
def download_single_image(img_url, folder_path, img_num, downloaded_hashes):
    """Download a single image - used by ThreadPoolExecutor"""
    try:
        if not img_url or img_url.startswith("data:image"):
            return (False, "invalid_url_or_data_uri", img_url)

        last_error = None
        # Retry a few times in case of transient network hiccups
        for _ in range(3):
            try:
                response = session.get(img_url, timeout=10)
                if response.status_code == 200 and response.content:
                    # img_hash = hashlib.md5(response.content).hexdigest()
                    # # Ensure thread-safe access to the shared set
                    # with download_hashes_lock:
                    #     if img_hash in downloaded_hashes:
                    #         return (False, "duplicate_hash", img_url)
                    #     downloaded_hashes.add(img_hash)

                    filename = os.path.join(folder_path, f"image_{img_num}.jpg")
                    with open(filename, "wb") as f:
                        f.write(response.content)
                    return (True, filename, img_url)
                else:
                    last_error = f"http_status_{response.status_code}"
            except Exception as e:
                last_error = e
                time.sleep(0.2)
        if last_error:
            logger.debug(f"Failed to download image {img_num}: {last_error}")
            return (False, f"failed_after_retries:{last_error}", img_url)
    except Exception as e:
        logger.debug(f"Failed to download image {img_num}: {e}")
        return (False, f"exception:{e}", img_url)
    return (False, "unknown_error", img_url)

# Changed: Logging to show Fail Downloads
# ---------- SCRAPE IMAGES WITH PARALLEL DOWNLOADS ----------
def scrape_property_images(driver, listing_id, base_image_folder, downloaded_hashes):
    """Optimized image scraping - no page reload, parallel downloads"""
    folder_path = os.path.join(base_image_folder, listing_id)
    os.makedirs(folder_path, exist_ok=True)
    
    try:
        # Try multiple selectors for image containers
        img_elements = []
        selectors = [
            "ul.splide__list img",
            ".carousel img", 
            ".gallery img",
            ".images img",
            "img[src*='image']"
        ]
        
        for selector in selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if elements:
                    img_elements = elements
                    break
            except:
                continue
        
        if not img_elements:
            return "not found"
        
        # Extract all image URLs first
        img_urls = []
        for img in img_elements:
            img_url = img.get_attribute("src") or img.get_attribute("data-src") or img.get_attribute("data-lazy-src")
            if img_url and not img_url.startswith("data:image"):
                img_urls.append(img_url)

        # Deduplicate URLs to avoid submitting the same image multiple times
        img_urls = list(dict.fromkeys(img_urls))
        
        if not img_urls:
            return "not found"
        
        print(f"📸 Downloading {len(img_urls)} images for {listing_id} in parallel...")
        
        # Use a per-listing hash set to avoid suppressing images across different properties
        if downloaded_hashes is None:
            downloaded_hashes = set()

        # Parallel download using ThreadPoolExecutor
        downloaded_count = 0
        failures = 0
        with ThreadPoolExecutor(max_workers=MAX_IMAGE_WORKERS) as executor:
            futures = {
                executor.submit(download_single_image, url, folder_path, i, downloaded_hashes): (i, url)
                for i, url in enumerate(img_urls, start=1)
            }

            for future in as_completed(futures):
                try:
                    success, detail, url = future.result()
                    if success:
                        downloaded_count += 1
                    else:
                        failures += 1
                        print(f"❌ Image download skipped/failed: {url} -> {detail}")
                except Exception as e:
                    failures += 1
                    idx, url = futures[future]
                    print(f"❌ Image download exception for {url}: {e}")
        
        print(f"✅ Downloaded {downloaded_count}/{len(img_urls)} images, failures: {failures}")
        return folder_path if downloaded_count > 0 else "not found"
        
    except Exception as e:
        logger.error(f"Failed to fetch images for {listing_id}: {e}")
        return "not found"

# ---------- SCRAPE DATA ----------
def scrape_homegate(driver, wait, base_url, base_image_folder):
    all_properties = []
    page_no = 1
    property_type = "Rent" if "/rent/" in base_url else "Buy"
    downloaded_hashes = set()
    seen_listings = set()  # Track already scraped listings
    MAX_RETRIES = 3
    RETRY_DELAY = 3
    attempt = 0

    # Changed Condition: Stop at page 50
    while page_no<=50:
        url = re.sub(r"ep=\d+", f"ep={page_no}", base_url)
        # Added: Exception
        try:
            driver.get(url)
            time.sleep(PAGE_LOAD_WAIT)
        except Exception as e:
            logger.error(f"Couldn't Load Page Url: {url} with error {e}")
        # Changed: Accept cookies If Present
        try:
            driver.find_element(By.ID, "onetrust-accept-btn-handler").click()
            time.sleep(0.5)
        except:
            pass
        
        # Quick error page check
        try:
            page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
            if any(phrase in page_text for phrase in [
                "requested page cannot be displayed",
                "page cannot be displayed",
                "no properties found",
                "keine objekte gefunden"
            ]):
                print(f"✅ Reached last page at page {page_no}")
                break
        except:
            pass
        
        # Changed Selector to Get property cards, Exception
        try:
            listings_box = driver.find_element(By.CSS_SELECTOR, 'div[data-test="result-list-container"]')
            # Correct CSS attribute selector and scope to the listings box
            cards = listings_box.find_elements(
                By.CSS_SELECTOR,
                "[role='listitem'] a[href*='/rent/'], [role='listitem'] a[href*='/buy/']"
            )
            print(f"Found {len(cards)} properties on this page")
        except Exception as e:
            print("⚠️ Could not load property cards", e)
            # Retry delay before next attempt
            attempt += 1
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
            else:
                logging.error(f"🚫 Failed after {MAX_RETRIES} attempts for {url}")
                break
        
        card_links = [c.get_attribute("href") for c in cards if c.is_displayed() and c.get_attribute("href")]
        card_links = list(dict.fromkeys(card_links))  # Remove duplicates

        if not card_links:
            print(f"✅ Reached last page (no properties found) at page {page_no}")
            break

        # Filter out already seen listings
        new_links = [link for link in card_links if link not in seen_listings]
        if not new_links:
            print(f"✅ No new properties on page {page_no}")
            break
        
        seen_listings.update(new_links)
        print(f"\n📄 Page {page_no}: Found {len(new_links)} new properties ({property_type})")

        for idx, property_url in enumerate(new_links, start=1):
            try:
                driver.get(property_url)
                time.sleep(PROPERTY_LOAD_WAIT)

                listing_id = property_url.split("/")[-1]
                
                title = safe_find(driver, By.CLASS_NAME, "ListingTitle_spotlightTitle_75f24")
                price = safe_find(driver, By.CLASS_NAME, 'SpotlightAttributesPrice_value_2af8f')
                rooms = safe_find(driver, By.CLASS_NAME, "SpotlightAttributesNumberOfRooms_value_a5947")
                living_space = safe_find(driver, By.CLASS_NAME, "SpotlightAttributesUsableSpace_value_48823")
                location = safe_find(driver, By.CLASS_NAME, 'AddressDetails_address_284e6')
                name = safe_find(driver, By.CLASS_NAME, 'ListerContactPhone_person_0c523')
                address = safe_find(driver, By.XPATH, "//div[contains(@class, 'ListingDetails_column')]/address")
                phone_number = safe_find(driver, By.CSS_SELECTOR, "a[href^='tel:']", attr="href").replace("tel:", "")
                description = safe_find(driver, By.CSS_SELECTOR, "div.Description_descriptionBody_3745e")
                object_ref = safe_find(driver, By.XPATH, "//dt[contains(text(), 'Object ref.')]/following-sibling::dd[1]")
                lat, lng = extract_coordinates_from_iframe(driver, wait)

                # Surroundings
                surroundings_str = "not found"
                try:
                    surrounding_list_element = driver.find_element(By.CSS_SELECTOR, "ul.TravelTime_travelTimeList_6208d")
                    items = surrounding_list_element.find_elements(By.TAG_NAME, "li")
                    surroundings_dict = {}
                    for item in items:
                        parts = item.text.strip().split("\n")
                        if len(parts) == 3:
                            category, time_str, place = parts
                            surroundings_dict[category] = f"{time_str} - {place}"
                    surroundings_str = "; ".join([f"{cat}: {info}" for cat, info in surroundings_dict.items()])
                except:
                    pass

                # Features
                try:
                    features_element = driver.find_element(By.CLASS_NAME, "FeaturesFurnishings_list_871ae")
                    features = features_element.text.replace("\n", ", ")
                except:
                    features = "not found"

                # Main info
                main_info = {}
                try:
                    main_info_div = driver.find_element(By.CSS_SELECTOR, "div.CoreAttributes_coreAttributes_fe6ae dl")
                    dt_elements = main_info_div.find_elements(By.TAG_NAME, "dt")
                    dd_elements = main_info_div.find_elements(By.TAG_NAME, "dd")
                    for dt, dd in zip(dt_elements, dd_elements):
                        main_info[dt.text.strip().replace(":", "")] = dd.text.strip()
                except:
                    pass

                # Scrape images (optimized - no page reload, parallel downloads)
                scrape_property_images(driver, listing_id, base_image_folder, downloaded_hashes)

                property_data = {
                    "Title": title,
                    "Price": price,
                    "Rooms": rooms,
                    "Living Space": living_space,
                    "Location": location,
                    "Surroundings": surroundings_str,
                    "Available From": main_info.get("Availability", "not found"),
                    "Type": main_info.get("Type", "not found"),
                    "No_of_rooms": main_info.get("No. of rooms", "not found"),
                    "Number of bathrooms": main_info.get("Number of bathrooms", "not found"),
                    "Surface Living": main_info.get("Living space", "not found"),
                    "Last Refurbishment": main_info.get("Last refurbishment", "not found"),
                    "Year Built": main_info.get("Year of construction", "not found"),
                    "Features": features,
                    "Description": description,
                    "Name": name,
                    "Full address": address,
                    "Phone": phone_number,
                    "Listing ID": listing_id,
                    "Object Reference": object_ref,
                    "Latitude": lat if lat else "not found",
                    "Longitude": lng if lng else "not found",
                    "Type (Rent/Buy)": property_type,
                    "URL": property_url,
                    "Website": base_url,
                    "Images": listing_id
                }

                all_properties.append(property_data)
                print(f"✅ [{idx}/{len(new_links)}] {title[:40]}...")

            except Exception as e:
                logger.error(f"Error scraping property {idx}: {e}")

        page_no += 1

    return all_properties

# ---------- SAVE DATA ----------
def save_data(data, save_dir, property_type):
    os.makedirs(save_dir, exist_ok=True)
    file_name = os.path.join(save_dir, f"homegate_{property_type}.csv")
    data_df = pd.DataFrame(data)
    data_df = data_df.drop_duplicates(subset=["Title"])
    data_df.to_csv(file_name, index=False)
    print(f"\n💾 {property_type} data saved to {file_name}")

# ---------- MAIN ----------
def main():
    start_time = time.time()
    print("🚀 Starting Homegate Scraper (OPTIMIZED VERSION)")
    print("="*60)
    print(f"⚡ Parallel image downloads: {MAX_IMAGE_WORKERS} workers")
    print(f"⚡ Reduced wait times: {REDUCED_WAIT_TIME}s (was 15s)")
    print(f"⚡ Page load wait: {PAGE_LOAD_WAIT}s (was 3s)")
    print("="*60)
    
    driver, wait = init_driver()
    
    save_dir = "./scraped_data/homegate/data"
    base_image_folder = "./scraped_data/homegate/images"
    os.makedirs(save_dir, exist_ok=True)
    os.makedirs(base_image_folder, exist_ok=True)
    
    print(f"📁 Excel files: {save_dir}")
    print(f"🖼️  Images: {base_image_folder}")
    print("="*60)

    # Added: URL Filters
    urls = [
        "https://www.homegate.ch/buy/real-estate/country-switzerland/matching-list?ep=1&be=50000",
        "https://www.homegate.ch/rent/real-estate/country-switzerland/matching-list?ep=1&be=50000"
    ]

    total_properties = 0
    
    try:
        for url in urls:
            url_start_time = time.time()
            print(f"\n🔗 Processing URL: {url}")
            
            data = scrape_homegate(driver, wait, url, base_image_folder)
            property_type = "Rent" if "/rent/" in url else "Buy"
            save_data(data, save_dir, property_type)
            
            total_properties += len(data)
            url_time = time.time() - url_start_time
            print(f"✅ {property_type} completed: {len(data)} properties in {url_time:.2f}s ({url_time/60:.2f}min)")
        
        end_time = time.time()
        total_time = end_time - start_time
        
        print(f"\n🎉 Homegate scraping completed!")
        print(f"📊 Total properties: {total_properties}")
        print(f"⏱️  Total time: {total_time:.2f}s ({total_time/60:.2f}min, {total_time/3600:.2f}hrs)")
        print(f"📈 Avg per property: {total_time/total_properties:.2f}s" if total_properties > 0 else "")
        
    except KeyboardInterrupt:
        print("\n⚠️ Interrupted by user")
    except Exception as e:
        print(f"\n❌ Failed with error: {e}")
        logger.exception(e)
    finally:
        print("🔚 Closing browser...")
        driver.quit()
        session.close()
        print("✅ Cleanup complete")

if __name__ == "__main__":
    main()
