import gspread
import asyncio
import json
import logging
from google.oauth2.service_account import Credentials
from tenacity import retry, wait_exponential, stop_after_attempt
from dotenv import load_dotenv
load_dotenv()
import os
import pandas as pd

import traceback

import time

# Define the scope and credentials
scopes = ["https://www.googleapis.com/auth/spreadsheets"]

# Lazy-initialized gspread client (supports both file and env var credentials)
_client = None


def _get_client():
    """Get or create the gspread client (lazy initialization)."""
    global _client
    if _client is not None:
        return _client

    # Option 1: GOOGLE_SERVICE_CRED env var (JSON string — used in Docker/production)
    cred_json = os.getenv("GOOGLE_SERVICE_CRED")
    if cred_json:
        try:
            info = json.loads(cred_json)
            creds = Credentials.from_service_account_info(info, scopes=scopes)
            _client = gspread.authorize(creds)
            return _client
        except Exception as e:
            logging.getLogger(__name__).error(f"Failed to load GOOGLE_SERVICE_CRED: {e}")

    # Option 2: credentials.json file (local development)
    if os.path.exists("credentials.json"):
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
        _client = gspread.authorize(creds)
        return _client

    logging.getLogger(__name__).warning(
        "No Google Sheets credentials found. Set GOOGLE_SERVICE_CRED env var or place credentials.json in agent/."
    )
    return None


# Keep backward-compatible 'client' reference (lazy)
class _LazyClient:
    """Proxy that initializes gspread client on first access."""
    def __getattr__(self, name):
        c = _get_client()
        if c is None:
            raise RuntimeError("Google Sheets client not configured. Set GOOGLE_SERVICE_CRED or add credentials.json.")
        return getattr(c, name)

client = _LazyClient()

# Cache for sheet data
_sheet_cache = {}
CACHE_TTL = 3600  # 1 hour


logging.basicConfig(
    level='INFO',
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Function to fetch all values from a sheet by name
# @retry(wait=wait_exponential(min=1, max=4), stop=stop_after_attempt(5))
def get_sheet_values(sheet_name, sheet_id=os.getenv("SHEET_ID")):
    global _sheet_cache
    
    # Check cache
    cache_key = f"{sheet_id}_{sheet_name}"
    now = time.time()
    
    if cache_key in _sheet_cache:
        cached_data, timestamp = _sheet_cache[cache_key]
        if now - timestamp < CACHE_TTL:
            # logger.info(f"Using cached data for sheet: {sheet_name}")
            return cached_data

    try:
        # Open the spreadsheet by key
        spreadsheet = client.open_by_key(sheet_id)

        # Access the sheet by name
        sheet = spreadsheet.worksheet(sheet_name)

        # Get all values from the sheet
        values = sheet.get_all_values()

        # Update cache
        _sheet_cache[cache_key] = (values, now)

        logger.info(f"{'='*50} {spreadsheet}")
        logger.info(f"{'='*10} {sheet}")
        logger.info(f"{'='*10} {values}")


        return values
    except gspread.exceptions.SpreadsheetNotFound:
        logger.info("The spreadsheet was not found.")
        return None
        create_or_update_issue("spreadsheet.get_sheet_values", "The spreadsheet was not found."+traceback.format_exc(), "error")
    except gspread.exceptions.WorksheetNotFound:
        logger.info(f"The sheet '{sheet_name}' was not found.")
        return None

# Async version of get_sheet_values
async def get_sheet_values_async(sheet_name, sheet_id=os.getenv("SHEET_ID")):
    """
    Async version of get_sheet_values that runs in a thread executor
    since gspread doesn't support async operations natively.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_sheet_values, sheet_name, sheet_id)
    
# @retry(wait=wait_exponential(min=1, max=4), stop=stop_after_attempt(5))
def get_sheet_as_dataframe(sheet_name, sheet_id=None):
    """
    Get all values from a Google Sheet and return as a pandas DataFrame.

    Args:
        sheet_name (str): Name of the sheet to retrieve
        sheet_id (str, optional): ID of the Google Sheet. If not provided, uses SHEET_ID from environment.

    Returns:
        pandas.DataFrame: DataFrame containing the sheet data, or None if an error occurs
    """
    try:
        # Get the raw values from the sheet
        values = get_sheet_values(sheet_name, sheet_id or os.getenv("SHEET_ID"))
        

        if not values:
            logger.info(f"No data found in sheet '{sheet_name}'")
            return None

        # Convert to DataFrame
        df = pd.DataFrame(values[1:], columns=values[0])
        return df

    except Exception as e:
        traceback.print_exc()
        logger.info(f"Error getting sheet as DataFrame: {str(e)}")
        return None

# Async version of get_sheet_as_dataframe
async def get_sheet_as_dataframe_async(sheet_name, sheet_id=None):
    """
    Async version of get_sheet_as_dataframe that runs in a thread executor.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_sheet_as_dataframe, sheet_name, sheet_id)

# @retry(wait=wait_exponential(min=1, max=4), stop=stop_after_attempt(5))
def append_to_sheet(sheet_id: str,sheet_name: str, data: list):
    """
    Append data to a specified sheet in a Google Spreadsheet.
    If the sheet does not exist, it will be created.

    Args:
        sheet_id (str): The ID of the Google Spreadsheet.
        sheet_name (str): The name of the sheet within the spreadsheet.
        data (list): A list of values to append to the sheet. For a single row, pass a list of values.
                     For multiple rows, pass a list of lists.

    Returns:
        dict: The response from the Google Sheets API.
    """
    try:

        
        # Open the spreadsheet by key
        spreadsheet = client.open_by_key(sheet_id)
        
        # Check if the sheet exists
        try:
            sheet = spreadsheet.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            # If the sheet does not exist, create it
            logger.info(f"Sheet '{sheet_name}' not found. Creating a new sheet...")
            row=len(data)
            col=len(data[0])
            sheet = spreadsheet.add_worksheet(title=sheet_name, rows=str(row+10), cols=str(col+5))
        
        # Append the data to the sheet
        response = sheet.append_rows(data)
        
        return response
    except gspread.exceptions.SpreadsheetNotFound:
        logger.info("The spreadsheet was not found.")
        return None
    except Exception as e:
        logger.info(f"An error occurred: {e}")
        return None
# @retry(wait=wait_exponential(min=1, max=4), stop=stop_after_attempt(5))
def write_to_sheet(sheet_name, data, fieldnames, sheet_id=None):
    """
    Write data to a Google Sheet, creating the sheet if it doesn't exist
    and clearing any existing data first.
    """
    try:
        # Get the spreadsheet
        spreadsheet = client.open_by_key(sheet_id or os.getenv("SHEET_ID"))
        
        # Check if the sheet exists, create if it doesn't
        try:
            sheet = spreadsheet.worksheet(sheet_name)
            # Clear existing data
            sheet.clear()
        except gspread.exceptions.WorksheetNotFound:
            # Create a new sheet with enough rows and columns
            sheet = spreadsheet.add_worksheet(
                title=sheet_name,
                rows=str(len(data) + 10),  # Add some extra rows
                cols=str(len(fieldnames) + 5)  # Add some extra columns
            )
        
        # Update the sheet with headers and data
        sheet.update([fieldnames] + [[row.get(field, '') for field in fieldnames] for row in data])
        
        logger.info(f"Successfully updated sheet '{sheet_name}' with {len(data)} rows.")
        return True
    except Exception as e:
        logger.info(f"Error writing to Google Sheet: {str(e)}")
        return False

# Async versions for remaining functions
async def append_to_sheet_async(sheet_id: str, sheet_name: str, data: list):
    """
    Async version of append_to_sheet that runs in a thread executor.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, append_to_sheet, sheet_id, sheet_name, data)

async def write_to_sheet_async(sheet_name, data, fieldnames, sheet_id=None):
    """
    Async version of write_to_sheet that runs in a thread executor.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, write_to_sheet, sheet_name, data, fieldnames, sheet_id)
