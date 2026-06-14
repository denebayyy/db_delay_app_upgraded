from fastapi import FastAPI

from urllib.parse import urljoin
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright

from lxml import html
import pandas as pd
import os

timestamp = datetime.now(ZoneInfo("Europe/Berlin")).strftime("%Y-%m-%d %H:%M:%S")
coming_from = "Übach-Palenberg"
going_to = "Hauptbahnhof, Aachen"

url=f"""https://www.bahn.de/buchung/fahrplan/suche#sts=true&so={coming_from}&zo={going_to}&kl=2&r=13:16:KLASSENLOS:1&soid=A%3D1%40O%3D%C3%9Cbach-Palenberg%40X%3D6097266%40Y%3D50924332%40U%3D80%40L%3D8005935%40p%3D1780342177%40i%3DU%C3%97008015189%40&zoid=A%3D1%40O%3DHauptbahnhof%2C%20Aachen%40X%3D6090767%40Y%3D50768755%40U%3D80%40L%3D501542%40p%3D1780342177%40i%3DU%C3%97028000993%40&sot=ST&zot=ST&soei=8005935&zoei=501542&hd={timestamp}&hza=D&hz=%5B%5D&ar=false&s=true&d=false&vm=00,01,02,03,04,06,07,08,09&fm=false&bp=false&dlt=false&dltv=false"""

file_path = "data.csv"

app = FastAPI(
    title="My FastAPI Backend",
    description="Starter FastAPI + Uvicorn setup",
    version="1.0.0"
)

@app.get("/")
def root():
    return {"message": "Backend is running 🚀"}


@app.get("/poll_db_data")
async def poll_db_data():
    # Access the browser to collect information regarding delays
    async with async_playwright() as p:
        browser = await p.chromium.launch() 

        page = await browser.new_page()

        page.set_default_timeout(120000)

        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=120000
        )

        print("Status:", response.status if response else None)
        print("Title:", await page.title())

        await page.wait_for_timeout(20000)

        print("TIME TAGS:", await page.locator("time").count())


        # Save the loaded html of the page locally to dissect later
        html = await page.content()
        with open("bahn.html", "w", encoding="utf-8") as f:
            f.write(html)

        preprocess()

def preprocess():
    # Load the html to dissect relevant parts out of it
    with open("bahn.html", encoding="utf-8") as f:
        tree = html.fromstring(f.read())

    # We are interested in the list elements that contain distinct trips
    times = tree.xpath("//li[.//time]")
    results = []

    # For each distinct trip extract necessary info out of the html
    for item in times:
        elements = item.xpath(".//time/text()")
        train = item.xpath(".//span[contains(@class, 'verbindungsabschnitt-visualisierung__verkehrsmittel-text')]/text()")
        cancellation = item.xpath(".//span[contains(@class, 'reise-ereignis-zusammenfassung__message-text test-reise-ereignis-zusammenfassung__text')]/text()")

        id = (train[0] if len(train) > 0 else None) + "_" + (elements[0] if len(elements) > 0 else None)

        # Get the time and if there are delays those as well
        query_time = timestamp
        planned_arrival = elements[0] if len(elements) > 0 else None
        actual_arrival = elements[1] if len(elements) > 1 else None
        
        planned_destination = elements[2] if len(elements) > 2 else None
        actual_destination = elements[3] if len(elements) > 3 else None
        
        #There is no delay and the green numbers have not yet appeared.
        if len(elements) == 2:
            planned_destination = actual_arrival
            actual_arrival = None

        train_info = train[0] if len(train) > 0 else None
        cancellation_info = True if len(cancellation) > 0 and cancellation[0] == "Verbindung fällt aus" else False

        results.append({
            "id": id,
            "query_time": query_time,
            "planned_arrival": planned_arrival,
            "actual_arrival": actual_arrival,
            "planned_destination": planned_destination,
            "actual_destination": actual_destination,
            "train": train_info,
            "cancellation": cancellation_info
        })

    df = pd.DataFrame(results)
    if os.path.exists(file_path):
        concat = pd.concat([pd.read_csv(file_path), df], ignore_index=True)
        concat.to_csv(file_path, index=False)
    else:
        df.to_csv(file_path, index=False)