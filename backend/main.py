"""Deutsche Bahn Train Delay Tracker - FastAPI Backend

This module implements a FastAPI backend for continuously polling Deutsche Bahn's
website for train schedule information and delays. It uses a job-based architecture
allowing multiple concurrent queries with different station pairs.

Architecture:
  - Scheduler: Low-level event scheduler based on asyncio
  - JobManager: High-level job orchestration with retry logic and status tracking
  - poll_station(): Worker function that performs the actual web scraping
  - preprocess(): Parses HTML and stores results to DB and CSV

Features:
  - Concurrent polling with configurable rate limits
  - Automatic retry with exponential backoff on failure
  - Per-job status tracking (running, error_count, run_count, etc.)
  - RESTful API for job management (create, start, stop, list)
  - Graceful startup/shutdown with proper resource cleanup

Dependencies:
  - FastAPI: Web framework
  - Playwright: Headless browser automation
  - lxml: HTML parsing
  - pandas: Data storage and CSV handling
  - psycopg2: PostgreSQL database connection
"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from urllib.parse import urljoin
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import locale
import asyncio
import logging

from playwright.async_api import async_playwright

from lxml import html
import pandas as pd
import os

from .db import db
from .db.scheduler import Scheduler
from .db.job_manager import JobManager, JobConfig

# Configure logging for this module
logger = logging.getLogger(__name__)

# Constants
date_format = "%Y-%m-%d %H:%M:%S"
file_path = "data.csv"

# Locale setup for German date parsing (e.g., "Mo. 15. Juni 2026")
try:
    locale.setlocale(locale.LC_TIME, "de_DE.UTF-8")
except locale.Error:
    locale.setlocale(locale.LC_TIME, "de-DE")

app = FastAPI(
    title="My FastAPI Backend",
    description="Starter FastAPI + Uvicorn setup",
    version="1.0.0"
)

# ============================================================================
# Global Initialization
# ============================================================================
# Create scheduler instance (manages job timing)
scheduler = Scheduler()
# Create job manager instance (orchestrates job lifecycle and execution)
job_manager = JobManager(scheduler, max_concurrent=3)


@app.get("/")
def root():
    return {"message": "Backend is running 🚀"}


# ============================================================================
# Poll Function (Job Worker)
# ============================================================================

async def poll_station(config: JobConfig):
    """Perform a single poll of Deutsche Bahn for train information.
    
    This is the main worker function executed by the JobManager for each
    recurring job. It uses Playwright to load the Bahn website, wait for
    data to load, save the HTML, and then parse it for train/delay info.
    
    Process:
    1. Build URL from config stations and current timestamp
    2. Launch Playwright browser and navigate to Bahn website
    3. Wait for page load and data rendering (20s safety buffer)
    4. Save HTML to file for backup/debugging
    5. Call preprocess() to extract train data
    6. Close browser and clean up resources
    
    Args:
        config: JobConfig containing from_station, to_station, and other params
        
    Raises:
        asyncio.TimeoutError: If page load exceeds 120s timeout
        Exception: Any other error during browser interaction or parsing
        
    Note:
        Logs are prefixed with config.id for traceability when running multiple jobs.
        Browser is always closed in finally block to prevent resource leaks.
    """
    timestamp = datetime.now(ZoneInfo("Europe/Berlin")).strftime("%Y-%m-%d %H:%M:%S")

    # Build URL with the config-specific stations
    # Note: The URL includes many encoded parameters for search filters
    url = f"""https://www.bahn.de/buchung/fahrplan/suche#sts=true&so={config.from_station}&zo={config.to_station}&kl=2&r=13:16:KLASSENLOS:1&soid=A%3D1%40O%3D%C3%9Cbach-Palenberg%40X%3D6097266%40Y%3D50924332%40U%3D80%40L%3D8005935%40p%3D1780342177%40i%3DU%C3%97008015189%40&zoid=A%3D1%40O%3DHauptbahnhof%2C%20Aachen%40X%3D6090767%40Y%3D50768755%40U%3D80%40L%3D501542%40p%3D1780342177%40i%3DU%C3%97028000993%40&sot=ST&zot=ST&soei=8005935&zoei=501542&hd={timestamp}&hza=D&hz=%5B%5D&ar=false&s=true&d=false&vm=00,01,02,03,04,06,07,08,09&fm=false&bp=false&dlt=false&dltv=false"""

    # Launch Playwright browser for web scraping
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            page.set_default_timeout(120000)  # 120s timeout per operation

            # Navigate to URL and wait for content to load
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=120000,
            )

            logger.info(f"[{config.id}] Status: {response.status if response else None}")
            logger.info(f"[{config.id}] Title: {await page.title()}")

            # Wait additional time for JavaScript to render train data
            await page.wait_for_timeout(20000)
            time_tags = await page.locator("time").count()
            logger.info(f"[{config.id}] TIME TAGS: {time_tags}")

            # Save the loaded HTML for backup and debugging
            page_html = await page.content()
            with open("bahn.html", "w", encoding="utf-8") as f:
                f.write(page_html)

            # Parse HTML and store results
            await preprocess(timestamp, config.id)

        finally:
            # Ensure browser is always closed to prevent resource leaks
            await browser.close()


async def preprocess(timestamp: str, job_id: str):
    """
    Parse HTML and extract train/delay information.
    Now job-aware for logging purposes.
    """
    # Load the HTML to dissect relevant parts
    with open("bahn.html", encoding="utf-8") as f:
        tree = html.fromstring(f.read())

    # We are interested in the list elements that contain distinct trips
    times = tree.xpath("//li[.//time]")
    results = []

    first_day = tree.xpath(
        ".//span[contains(@class, 'default-reiseloesung-list-page-controls__title-date')]/text()"
    )
    second_day = tree.xpath(
        ".//div[contains(@class, 'reiseloesung-heading')]/text()"
    )

    # For each distinct trip extract necessary info
    for item in times:
        # Get the time that is displayed
        elements = item.xpath(".//time/text()")
        # Get the respective train name
        train = item.xpath(
            ".//span[contains(@class, 'verbindungsabschnitt-visualisierung__verkehrsmittel-text')]/text()"
        )
        # Get information such as cancellation or warnings
        cancellation = item.xpath(
            ".//span[contains(@class, 'reise-ereignis-zusammenfassung__message-text test-reise-ereignis-zusammenfassung__text')]/text()"
        )

        id = (
            (train[0] if len(train) > 0 else None)
            + "_"
            + (elements[0] if len(elements) > 0 else None)
        )

        # Get the time and if there are delays those as well
        query_time = timestamp
        planned_arrival = elements[0] if len(elements) > 0 else None
        actual_arrival = elements[1] if len(elements) > 1 else None

        planned_destination = elements[2] if len(elements) > 2 else None
        actual_destination = elements[3] if len(elements) > 3 else None

        # There is no delay and the green numbers have not yet appeared.
        if len(elements) == 2:
            planned_destination = actual_arrival
            actual_arrival = None

        train_info = train[0] if len(train) > 0 else None
        # Store whether the train has been cancelled or not
        cancellation_info = (
            True
            if len(cancellation) > 0 and cancellation[0] == "Verbindung fällt aus"
            else False
        )
        meldung_info = cancellation[0] if len(cancellation) > 0 else None

        # Get the date of the first day
        date_time = datetime.strptime(first_day[0], "%a. %d. %B %Y")
        first_day_data_prefix = str(date_time)[:10]

        # Naively assign all the data
        planned_arrival_ts = datetime.strptime(
            first_day_data_prefix + " " + planned_arrival + ":00", date_format
        )
        if actual_arrival is not None:
            actual_arrival_ts = datetime.strptime(
                first_day_data_prefix + " " + actual_arrival + ":00", date_format
            )
        else:
            actual_arrival_ts = None

        planned_destination_ts = datetime.strptime(
            first_day_data_prefix + " " + planned_destination + ":00", date_format
        )
        if actual_destination is not None:
            actual_destination_ts = datetime.strptime(
                first_day_data_prefix + " " + actual_destination + ":00", date_format
            )
        else:
            actual_destination_ts = None

        # Handle trips crossing midnight
        if (
            actual_arrival is not None
            and (planned_arrival_ts - actual_arrival_ts).total_seconds() > 8 * 3600
        ):
            actual_arrival_ts = datetime.strptime(
                first_day_data_prefix + " " + actual_arrival + ":00", date_format
            ) + timedelta(days=1)

        if (planned_arrival_ts - planned_destination_ts).total_seconds() > 8 * 3600:
            planned_destination_ts = datetime.strptime(
                first_day_data_prefix + " " + planned_destination + ":00", date_format
            ) + timedelta(days=1)

        if (
            actual_destination is not None
            and (planned_arrival_ts - actual_destination_ts).total_seconds()
            > 8 * 3600
        ):
            actual_destination_ts = datetime.strptime(
                first_day_data_prefix + " " + actual_destination + ":00", date_format
            ) + timedelta(days=1)

        # Handle trips for the following day
        if second_day != [] and item != times[0]:
            second_date_time = datetime.strptime(second_day[0], "%a. %d. %B %Y")
            second_day_data_prefix = str(second_date_time)[:10]

            first_item_planned_arrival = results[0]["planned_arrival"]
            # 10 hours threshold
            if (
                first_item_planned_arrival - planned_arrival_ts
            ).total_seconds() > 10 * 3600:
                planned_arrival_ts = datetime.strptime(
                    second_day_data_prefix + " " + planned_arrival + ":00", date_format
                )
                planned_destination_ts = datetime.strptime(
                    second_day_data_prefix + " " + planned_destination + ":00",
                    date_format,
                )

                if actual_arrival is not None:
                    actual_arrival_ts = datetime.strptime(
                        second_day_data_prefix + " " + actual_arrival + ":00",
                        date_format,
                    )
                if actual_destination is not None:
                    actual_destination_ts = datetime.strptime(
                        second_day_data_prefix + " " + actual_destination + ":00",
                        date_format,
                    )

        new_obj = {
            "id": id,
            "query_time": query_time,
            "planned_arrival": planned_arrival_ts,
            "actual_arrival": actual_arrival_ts,
            "planned_destination": planned_destination_ts,
            "actual_destination": actual_destination_ts,
            "train": train_info,
            "cancellation": cancellation_info,
            "trip_information": meldung_info,
        }
        # Insert the data into the db
        results.append(new_obj)
        db.insert_data(new_obj)

    # Save locally
    df = pd.DataFrame(results)
    if os.path.exists(file_path):
        concat = pd.concat([pd.read_csv(file_path), df], ignore_index=True)
        concat.to_csv(file_path, index=False)
    else:
        df.to_csv(file_path, index=False)

    logger.info(f"[{job_id}] Processed {len(results)} trips")


# ============================================================================
# Scheduler Lifecycle Events
# ============================================================================

@app.on_event("startup")
async def startup_event():
    """Initialize scheduler and load persisted jobs on application startup.
    
    This handler is called once when the FastAPI application starts.
    It:
    1. Wires the poll_station() function to the JobManager
    2. Starts the background scheduler task
    3. Loads all saved job configurations from the database
    
    Jobs are persisted across restarts, so this ensures continuous polling.
    
    Errors during job loading are logged but don't crash the app;
    jobs can be created later via the POST /jobs API endpoint.
    """
    logger.info("Starting scheduler...")

    # Wire the poll function to the job manager
    # This must be done before any jobs are created
    job_manager.set_poll_func(poll_station)

    # Start the scheduler in the background as an asyncio task
    # The scheduler runs indefinitely until shutdown
    asyncio.create_task(scheduler.run())

    # Load all saved jobs from database
    try:
        saved_jobs = db.load_all_jobs()
        for job_row in saved_jobs:
            job_config = JobConfig(
                id=job_row[0],
                from_station=job_row[1],
                to_station=job_row[2],
                interval=job_row[3],
                enabled=job_row[4],
                max_retries=job_row[5],
                timeout=job_row[6]
            )
            await job_manager.create_job(job_config)
        logger.info(f"Loaded {len(saved_jobs)} jobs from database")
    except Exception as e:
        logger.error(f"Failed to load jobs from database: {e}")


@app.on_event("shutdown")
async def shutdown_event():
    """Gracefully shutdown all scheduled jobs on application shutdown.
    
    Called when the FastAPI application is shutting down (e.g., SIGTERM).
    Ensures all background tasks are cancelled and resources are cleaned up.
    """
    logger.info("Shutting down...")
    await job_manager.shutdown()


# ============================================================================
# Job Management API Endpoints
# ============================================================================

@app.post("/jobs")
async def create_job(config: JobConfig):
    """Create and optionally start a new polling job.
    
    Accepts a JobConfig JSON body specifying the route and polling parameters.
    Job configuration is persisted to the database for recovery after restarts.
    
    Request Body (example):
        {
            "id": "cologne-berlin",
            "from_station": "Köln Hbf",
            "to_station": "Berlin Hbf",
            "interval": 600.0,
            "enabled": true,
            "max_retries": 3,
            "timeout": 120.0
        }
    
    Returns:
        201: Job created successfully
        400: Invalid config or job_id already exists
        
    Side Effects:
        If enabled=true, job begins polling immediately at the specified interval.
        Job configuration is saved to database for persistence.
    """
    try:
        await job_manager.create_job(config)
        db.save_job(config)
        return JSONResponse(
            status_code=201,
            content={"status": "created", "job_id": config.id},
        )
    except Exception as e:
        logger.error(f"Failed to create job: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/jobs/{job_id}/start")
async def start_job(job_id: str):
    """Resume a stopped job (starts recurring execution).
    
    Used to re-enable a job that was previously stopped.
    Idempotent: calling this on an already-running job returns success.
    
    Args:
        job_id: Unique identifier of the job to start
        
    Returns:
        200: Job started (or already running)
        400: Job not found or start failed
    """
    try:
        await job_manager.start_job(job_id)
        return {"status": "started", "job_id": job_id}
    except Exception as e:
        logger.error(f"Failed to start job: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/jobs/{job_id}/run")
async def run_job_once(job_id: str):
    """Trigger a single poll immediately (one-off execution).
    
    Executes the job outside its normal schedule. Useful for testing,
    manual triggers, or forcing a refresh. Does not affect the recurring
    schedule or status metrics.
    
    Args:
        job_id: Unique identifier of the job to execute
        
    Returns:
        200: Poll triggered and completed (or failed with retries)
        400: Job not found
        
    Note:
        This call is async but returns after the poll completes (up to timeout).
    """
    try:
        await job_manager.run_once(job_id)
        return {"status": "triggered", "job_id": job_id}
    except Exception as e:
        logger.error(f"Failed to run job: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    """Stop and permanently remove a job.
    
    Cancels recurring execution and removes all configuration and status data.
    Also removes the job from the database to prevent it from being reloaded on restart.
    If the job doesn't exist, still returns success (idempotent).
    
    Args:
        job_id: Unique identifier of the job to delete
        
    Returns:
        200: Job deleted (or didn't exist)
        400: Deletion failed for other reason
    """
    try:
        await job_manager.stop_job(job_id)
        if job_id in job_manager.jobs:
            del job_manager.jobs[job_id]
        if job_id in job_manager.statuses:
            del job_manager.statuses[job_id]
        db.delete_job_config(job_id)
        return {"status": "deleted", "job_id": job_id}
    except Exception as e:
        logger.error(f"Failed to delete job: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/jobs/{job_id}/status")
def get_job_status(job_id: str):
    """Retrieve the current execution status of a job.
    
    Returns real-time metrics: running state, last run time, error count, etc.
    
    Args:
        job_id: Unique identifier of the job
        
    Returns (example):
        {
            "job_id": "cologne-berlin",
            "running": false,
            "last_run": "2026-06-26T14:30:45.123456",
            "last_error": null,
            "error_count": 0,
            "run_count": 12
        }
        
    Returns:
        200: Status retrieved
        404: Job not found
    """
    status = job_manager.get_status(job_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return status.to_dict()


@app.get("/jobs")
def list_all_jobs():
    """Retrieve status of all managed jobs.
    
    Returns a dictionary mapping each job_id to its current status.
    Useful for dashboards, monitoring, or bulk status checks.
    
    Returns (example):
        {
            "default-aachen-home": {
                "job_id": "default-aachen-home",
                "running": true,
                "last_run": "2026-06-26T14:35:12.654321",
                "last_error": null,
                "error_count": 0,
                "run_count": 5
            },
            "cologne-berlin": {
                "job_id": "cologne-berlin",
                "running": false,
                "last_run": "2026-06-26T14:32:00",
                "last_error": "Timeout",
                "error_count": 1,
                "run_count": 3
            }
        }
    """
    return {
        job_id: status.to_dict()
        for job_id, status in job_manager.list_jobs().items()
    }


# ============================================================================
# Data Retrieval Endpoints
# ============================================================================

@app.get("/get_all_the_data")
def get_all_data():
    """Retrieve all collected train data from the database.
    
    Returns the complete history of all polls, including planned/actual times,
    delays, cancellations, and other metadata.
    
    Returns (example):
        {
            "count": 42,
            "data": [
                {
                    "id": "RB_10:45",
                    "query_time": "2026-06-26 14:35:12",
                    "planned_arrival": "2026-06-26 10:45:00",
                    "actual_arrival": "2026-06-26 10:51:00",
                    "planned_destination": "2026-06-26 11:30:00",
                    "actual_destination": "2026-06-26 11:37:00",
                    "train": "RB",
                    "cancellation": false,
                    "trip_information": null
                },
                ...
            ]
        }
    
    Note:
        This endpoint fetches from the database directly (via db.get_data_debug()).
        For large datasets, consider adding pagination or filtering in the future.
    """
    data = db.get_data_debug()
    return {"count": len(data) if data else 0, "data": data}
