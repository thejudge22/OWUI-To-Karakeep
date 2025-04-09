#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Syncs OpenWebUI chats to a Karakeep/Hoarder instance.

Reads chat metadata and message history stored as a JSON blob
within the 'chat' column of the OpenWebUI 'chat' table.
Uses timestamp-based syncing (epoch seconds for DB, ISO for state file).
Embeds the OpenWebUI Chat ID into the Karakeep item title for tracking.
Interacts with Karakeep API v1 using bookmark endpoints.
"""

import sqlite3
import requests
import json
import time
import os
import re # For title parsing
import traceback # For detailed error logging
from datetime import datetime, timezone

# --- Configuration ---

# --- Database Configuration (Choose ONE block) ---

# Option 1: SQLite (Default for OpenWebUI Docker)
OPENWEBUI_DB_TYPE = 'sqlite'
# IMPORTANT: Replace with the *actual* path to your database file
OPENWEBUI_DB_PATH = '/path/to/owui/dbfile/webui.db' # EXAMPLE PATH
# PG_CONFIG = {} # Not used for SQLite

# Option 2: PostgreSQL (If configured for OpenWebUI)
# OPENWEBUI_DB_TYPE = 'postgres'
# OPENWEBUI_DB_PATH = None # Not used for Postgres
# PG_CONFIG = {
#     'dbname': 'webui',         # Replace with your DB name if different
#     'user': 'db_user',         # Replace with your DB user
#     'password': 'db_password', # Replace with your DB password
#     'host': 'localhost',       # Replace with your DB host if not local
#     'port': '5432'             # Replace with your DB port if not default
# }
# --- End Database Configuration ---


# --- Karakeep/Hoarder Configuration ---
# Replace with the URL of your Karakeep/Hoarder API endpoint (including /api/v1)
KARAKEEP_API_URL = 'http://localhost:3000/api/v1' # EXAMPLE URL
# Generate an API Key in Karakeep and paste it here
KARAKEEP_API_KEY = 'ak1_3faaabbbbbb' # EXAMPLE KEY - REPLACE
# The name of the list in Karakeep where chats will be stored
TARGET_LIST_NAME = 'Chats' #Example List name
# --- End Karakeep Configuration ---


# --- Script State & Title ID Format ---
# File to store the timestamp of the last successful sync
STATE_FILE = 'sync_state_title_id.json'
# A very old timestamp (Jan 1 1970) represented as epoch seconds for DB query comparison
EPOCH_ZERO_SECONDS = 0
# Default ISO timestamp string for state file if it doesn't exist or is invalid
INITIAL_STATE_ISO = "1970-01-01T00:00:00.000Z"
# Format for embedding OpenWebUI Chat ID into the Karakeep Item Title
TITLE_ID_PREFIX = "[OW_ID:"
TITLE_ID_SUFFIX = "]" # Note the space after ']' for readability
# Maximum allowed length for item titles in Karakeep (adjust if needed)
MAX_KARAKEEP_TITLE_LENGTH = 255
# --- End Script State & Title ID Format ---


# --- Helper Functions ---

def get_db_connection():
    """Establishes connection to the configured database (SQLite or PostgreSQL)."""
    conn = None
    try:
        if OPENWEBUI_DB_TYPE == 'sqlite':
            if not OPENWEBUI_DB_PATH or not os.path.exists(OPENWEBUI_DB_PATH):
                raise FileNotFoundError(f"SQLite DB not found or path not set: {OPENWEBUI_DB_PATH}")
            # Using detect_types might help later if schema changes, but not strictly needed for epoch integers
            conn = sqlite3.connect(OPENWEBUI_DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
            # Return rows as dictionary-like objects for easier access by column name
            conn.row_factory = sqlite3.Row
            print("Connected to SQLite DB")
        elif OPENWEBUI_DB_TYPE == 'postgres':
            try:
                import psycopg2
                import psycopg2.extras
            except ImportError:
                print("ERROR: psycopg2 library not found but needed for PostgreSQL.")
                print("Please install it: pip install psycopg2-binary")
                return None # Cannot proceed

            conn = psycopg2.connect(**PG_CONFIG)
            # DictCursor will be applied when creating cursors
            print("Connected to PostgreSQL DB")
        else:
            raise ValueError("Invalid OPENWEBUI_DB_TYPE configured. Choose 'sqlite' or 'postgres'.")
        return conn
    except FileNotFoundError as e:
         print(f"ERROR: Database file not found. {e}")
         return None
    except Exception as e:
        # Catch other potential connection errors (credentials, host unreachable, etc.)
        print(f"Error connecting to {OPENWEBUI_DB_TYPE} DB: {e}")
        return None

def load_sync_state():
    """Loads the last sync timestamp (ISO format string) from the state file."""
    # Use the correct variable for the initial ISO string
    default_state = {'last_sync_timestamp': INITIAL_STATE_ISO}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            try:
                state = json.load(f)
                if 'last_sync_timestamp' not in state:
                    print(f"Warning: State file {STATE_FILE} missing 'last_sync_timestamp'. Starting fresh.")
                    return default_state
                # Basic validation: Check if it's a string and can be parsed as ISO datetime
                ts_str = state['last_sync_timestamp']
                if not isinstance(ts_str, str):
                     raise TypeError("Timestamp in state file is not a string.")
                datetime.fromisoformat(ts_str.replace('Z', '+00:00')) # Attempt parse
                print(f"Loaded state: Timestamp={state['last_sync_timestamp']}")
                return state
            except (json.JSONDecodeError, ValueError, TypeError) as e:
                 print(f"Warning: State file {STATE_FILE} invalid ({e}). Starting fresh.")
                 return default_state # Return default state if file is corrupt or invalid format
    print(f"State file {STATE_FILE} not found. Starting fresh.")
    return default_state

def save_sync_state(state):
    """Saves the sync state (with timestamp as ISO string) to the state file."""
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2) # Use indent for readability
        print(f"Saved sync state. Last timestamp: {state.get('last_sync_timestamp')}")
    except IOError as e:
        print(f"ERROR: Could not write to state file {STATE_FILE}: {e}")

def get_karakeep_headers():
    """Returns the necessary HTTP headers for Karakeep API requests."""
    return {
        'Authorization': f'Bearer {KARAKEEP_API_KEY}',
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }

def find_or_create_karakeep_list(list_name):
    """Finds the ID of the target Karakeep list by name, creating it if it doesn't exist."""
    headers = get_karakeep_headers()
    # Construct the specific v1 endpoint URL for lists
    list_api_url = f"{KARAKEEP_API_URL}/lists" # Base URL likely includes /api/v1

    list_id = None
    print(f"Checking for Karakeep list named '{list_name}' at {list_api_url} ...")

    try:
        # GET request to find the list (fetch all lists - Hoarder may not paginate /lists)
        response_get = requests.get(list_api_url, headers=headers, params={'per_page': 1000}, timeout=15) # Keep per_page just in case
        response_get.raise_for_status()
        lists_data = response_get.json()

        # Expecting format like {"lists": [...]} based on earlier debug output
        possible_lists = []
        if isinstance(lists_data, dict) and isinstance(lists_data.get('lists'), list):
            possible_lists = lists_data.get('lists')
            # print("DEBUG: Parsing response as a dict with list under 'lists' key.")
        else:
            print(f"Warning: Could not find list of lists under 'lists' key in response from {list_api_url}. Response structure: {str(lists_data)[:200]}...")

        # Now iterate through the determined list of lists
        # print(f"DEBUG: Iterating through {len(possible_lists)} potential lists found in response...")
        for lst in possible_lists:
            if not isinstance(lst, dict):
                print(f"Warning: Item in list is not a dictionary: {lst}")
                continue
            current_list_name = lst.get('name')
            current_list_id = lst.get('id')
            if current_list_name == list_name:
                list_id = current_list_id
                print(f"Found Karakeep list '{list_name}' with ID: {list_id}")
                return list_id # Found it!

        # If loop finishes and list_id is still None, proceed to create
        if not list_id:
            print(f"List '{list_name}' not found after checking {len(possible_lists)} items. Creating...")
            payload_dict = { "name": list_name, "icon": "list" } # Use appropriate icon if desired
            payload = json.dumps(payload_dict)
            response_post = requests.post(list_api_url, headers=headers, data=payload, timeout=15)
            response_post.raise_for_status()
            response_json = response_post.json()

            # ID might be nested under 'list' key upon creation? Check API docs. Assuming top-level 'id' for now.
            list_id = response_json.get('id')
            if not list_id and isinstance(response_json.get('list'), dict):
                 list_id = response_json.get('list', {}).get('id') # Check nested possibility

            if list_id:
                print(f"Created Karakeep list '{list_name}' with ID: {list_id}")
                return list_id
            else:
                print(f"Error: List creation POST succeeded but 'id' field not found in response: {response_post.text}")
                return None

    except requests.exceptions.RequestException as e:
        print(f"Error interacting with Karakeep API ({list_api_url}): {e}")
        response_to_log = getattr(e, 'response', None)
        if response_to_log is not None:
             print(f"Response Status: {response_to_log.status_code}")
             try: print(f"Response Text: {response_to_log.text[:200]}...")
             except Exception: pass
        return None
    except Exception as e:
        print(f"An unexpected error occurred finding/creating list '{list_name}': {e}")
        traceback.print_exc()
        return None

    return list_id # Should have returned earlier if successful

def format_conversation(messages):
    """
    Formats a list of chat messages (from parsed JSON) into a readable string (Markdown).
    Handles epoch timestamps (attempts ms, falls back to seconds).
    """
    conversation = ""
    if not isinstance(messages, list):
         print(f"Warning: format_conversation expected a list, got {type(messages)}. Returning empty string.")
         return ""

    for msg in messages:
        # Extract data safely using .get() with defaults from JSON structure
        role = msg.get('role', 'Unknown')
        content = msg.get('content', '')
        ts_epoch_val = msg.get('timestamp') # Timestamp from JSON
        ts_str = "Timestamp N/A"

        if isinstance(ts_epoch_val, (int, float)):
            try:
                # Check if it looks like milliseconds (heuristic)
                if ts_epoch_val > 3_000_000_000: # If > ~2065, assume ms
                    ts_epoch_sec = ts_epoch_val / 1000.0
                else:
                    ts_epoch_sec = float(ts_epoch_val) # Assume seconds

                ts_obj = datetime.fromtimestamp(ts_epoch_sec, timezone.utc)
                ts_str = ts_obj.strftime('%Y-%m-%d %H:%M:%S %Z') # Format as UTC string
            except Exception as ts_e:
                print(f"Warning: Could not format timestamp epoch value {ts_epoch_val}: {ts_e}")
                ts_str = f"Epoch: {ts_epoch_val}" # Fallback
        elif ts_epoch_val is not None:
             print(f"Warning: Unexpected type for message timestamp in JSON: {type(ts_epoch_val)}, value: {ts_epoch_val}")
             ts_str = str(ts_epoch_val)

        conversation += f"**{role.capitalize()}** ({ts_str}):\n{content}\n\n---\n\n"
    return conversation.strip()

def get_karakeep_item_map_by_title(list_id):
    """
    Queries Karakeep list's bookmarks using cursor pagination,
    filters by title prefix, returns {ow_chat_id: karakeep_bookmark_id} map.
    """
    item_map = {}
    headers = get_karakeep_headers()
    current_cursor = None
    has_more_pages = True
    print(f"Building map of existing Karakeep bookmarks in list {list_id} by title prefix...")

    # --- START MODIFICATION ---

    # Regex to find " [OW_ID:<ID>]" at the end ($) of the title.
    # Uses capture group ([a-zA-Z0-9\-]+) for UUID-like IDs.
    # Note the leading space before the escaped prefix.
    # TITLE_ID_SUFFIX was already changed globally to not have a trailing space.
    title_pattern_str = f" {re.escape(TITLE_ID_PREFIX)}([a-zA-Z0-9\\-]+){re.escape(TITLE_ID_SUFFIX)}$"
    # Escape the hyphen inside [] in the f-string requires double backslash \\-
    title_regex = re.compile(title_pattern_str)
    
    items_processed = 0
    page_num_for_logging = 1

    while has_more_pages:
        # Use the correct endpoint for bookmarks in a list
        url = f"{KARAKEEP_API_URL}/lists/{list_id}/bookmarks"
        params = {}
        if current_cursor:
            params['cursor'] = current_cursor
        # Optional: Add 'limit' if supported: params['limit'] = 100

        # print(f"Fetching page {page_num_for_logging} (Cursor: {current_cursor})...") # Verbose logging

        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()

            # Extract list based on confirmed response structure {"bookmarks": [...]}
            items = data.get('bookmarks', [])
            if not isinstance(items, list):
                 print(f"Warning: Expected a list under 'bookmarks' key, but got {type(items)}. Response: {str(data)[:200]}...")
                 items = []

            if not items:
                # print(f"No bookmarks found on page {page_num_for_logging}.") # Verbose logging
                break # Exit loop if no items are returned

            # print(f"Processing {len(items)} bookmarks from page {page_num_for_logging}...") # Verbose logging
            for item in items:
                items_processed += 1
                # Extract ID and Title directly from bookmark object
                kk_item_id = item.get('id')
                title = item.get('title', '') # Default to empty string if null/missing

                if not isinstance(title, str): title = "" # Handle non-string titles
                if not kk_item_id:
                    print(f"Warning: Found bookmark without an ID: {str(item)[:100]}... Skipping.")
                    continue

                match = title_regex.match(title)
                if match:
                    ow_chat_id = match.group(1)
                    item_map[ow_chat_id] = str(kk_item_id)

            # Update cursor for next iteration based on {"nextCursor": ...}
            current_cursor = data.get('nextCursor')
            if not current_cursor:
                has_more_pages = False
                # print("No nextCursor found, assuming end of results.") # Verbose logging
            else:
                 page_num_for_logging += 1

            time.sleep(0.1) # Reduce sleep slightly if API is fast

        except requests.exceptions.Timeout:
            print(f"Warning: Timeout occurred while fetching bookmarks (Cursor: {current_cursor}) from Karakeep ({url}). Retrying...")
            time.sleep(5)
            continue
        except requests.exceptions.RequestException as e:
            print(f"Error fetching bookmark map from Karakeep ({url}, Cursor: {current_cursor}): {e}")
            if hasattr(e, 'response') and e.response is not None:
                 print(f"Response Status: {e.response.status_code}, Response Text: {e.response.text[:200]}...")
            has_more_pages = False; break
        except json.JSONDecodeError as e:
            print(f"Error decoding JSON response from Karakeep ({url}, Cursor: {current_cursor}): {e}")
            if hasattr(e, 'doc'): print(f"Invalid JSON received: {e.doc[:200]}...")
            has_more_pages = False; break
        except Exception as e:
             print(f"An unexpected error occurred processing Karakeep bookmarks (Cursor: {current_cursor}): {e}")
             traceback.print_exc(); has_more_pages = False; break

    print(f"Finished building map. Processed {items_processed} Karakeep bookmarks. Found {len(item_map)} matching items.")
    return item_map

def sync_or_update_chat_in_karakeep(chat_row, formatted_conversation_text, list_id, existing_bookmark_id):
    """
    Creates or Updates a bookmark globally ('text' type) using chat metadata
    and pre-formatted conversation text, ensures it's linked to the list.
    """
    headers = get_karakeep_headers()
    ow_chat_id = str(chat_row['id']) # Use ID from the chat table row
    original_title = chat_row['title'] if chat_row['title'] else f"Untitled Chat {ow_chat_id}"
    if not original_title: original_title = f"Untitled Chat {ow_chat_id}"

# --- START MODIFICATION ---

    # Construct the ID tag suffix (e.g., " [OW_ID:xyz]")
    # Note the leading space before the prefix constant
    id_tag_suffix = f" {TITLE_ID_PREFIX}{ow_chat_id}{TITLE_ID_SUFFIX}"

    # Calculate available length for the original title part
    available_title_length = MAX_KARAKEEP_TITLE_LENGTH - len(id_tag_suffix)
    if available_title_length < 0:
        # Handle edge case where ID tag itself exceeds max length (unlikely but safe)
        print(f"Warning: OW_ID tag '{id_tag_suffix}' alone exceeds MAX_KARAKEEP_TITLE_LENGTH ({MAX_KARAKEEP_TITLE_LENGTH}). Truncating tag.")
        id_tag_suffix = id_tag_suffix[:MAX_KARAKEEP_TITLE_LENGTH]
        truncated_title = ""
        available_title_length = 0 # Prevent negative indexing later
    else:
        # Truncate the original title if it's too long
        # Using max(0, ...) ensures we don't use negative slicing if available_title_length is small
        if len(original_title) > available_title_length:
            # Truncate and add ellipsis if space allows (at least 3 chars needed for ellipsis)
            if available_title_length >= 3:
                 truncated_title = original_title[:max(0, available_title_length - 3)] + "..."
            else: # Not enough space even for ellipsis, just truncate hard
                 truncated_title = original_title[:available_title_length]
        else:
            truncated_title = original_title # No truncation needed

    # Combine the truncated title and the ID tag suffix
    combined_title = f"{truncated_title}{id_tag_suffix}"

    # Prepare payload using formatted text
    payload_dict = {
        "title": combined_title,
        "text": formatted_conversation_text, # Use formatted text from JSON blob
        "type": "text",
        "note": "", # Add other metadata if desired
        "summary": "",
        "archived": False,
        "favourited": False,
    }
    payload_json = json.dumps(payload_dict)

    success = False
    operation = "UNKNOWN"

    try:
        if existing_bookmark_id:
            # --- UPDATE existing bookmark ---
            operation = "UPDATE"
            update_url = f"{KARAKEEP_API_URL}/bookmarks/{existing_bookmark_id}"
            # print(f"Attempting {operation} Bookmark for OW Chat ID {ow_chat_id} (Bookmark ID: {existing_bookmark_id}) at {update_url}")
            response = requests.put(update_url, headers=headers, data=payload_json, timeout=20)

            if response.status_code == 404:
                 print(f"Warning: Bookmark {existing_bookmark_id} not found during PUT {update_url}. Attempting to CREATE.")
                 existing_bookmark_id = None
                 operation = "CREATE (after UPDATE 404)"
            else:
                 response.raise_for_status()
                 print(f"Successfully {operation}D Bookmark for OW Chat ID {ow_chat_id} (Bookmark ID: {existing_bookmark_id})")
                 success = True

        if not existing_bookmark_id:
            # --- CREATE new bookmark (2-step process) ---
            if operation != "CREATE (after UPDATE 404)": operation = "CREATE"

            # Step 1: Create global bookmark
            create_url = f"{KARAKEEP_API_URL}/bookmarks"
            # print(f"Attempting {operation} (Step 1/2): Create global bookmark for OW Chat ID {ow_chat_id} at {create_url}")
            response_create = requests.post(create_url, headers=headers, data=payload_json, timeout=20)
            response_create.raise_for_status()
            created_bookmark_data = response_create.json()
            new_bookmark_id = created_bookmark_data.get('id')
            if not new_bookmark_id:
                print(f"ERROR: Could not extract 'id' from POST /bookmarks response for OW Chat ID {ow_chat_id}. Response: {str(created_bookmark_data)[:500]}...")
                return False
            new_bookmark_id = str(new_bookmark_id)
            print(f"Successfully {operation}D (Step 1/2): Created Bookmark ID {new_bookmark_id} for OW Chat ID {ow_chat_id}")

            # Step 2: Link the new bookmark to the target list
            link_url = f"{KARAKEEP_API_URL}/lists/{list_id}/bookmarks/{new_bookmark_id}"
            # print(f"Attempting {operation} (Step 2/2): Link Bookmark ID {new_bookmark_id} to List ID {list_id} at {link_url}")
            response_link = requests.put(link_url, headers=headers, data=json.dumps({}), timeout=15) # Send empty JSON body
            response_link.raise_for_status()
            print(f"Successfully {operation}D (Step 2/2): Linked Bookmark ID {new_bookmark_id} to List ID {list_id}")
            success = True

    # --- Error Handling ---
    except requests.exceptions.Timeout:
        print(f"ERROR: Timeout during {operation} for OW Chat ID {ow_chat_id}.")
        success = False
    except requests.exceptions.RequestException as e:
        print(f"ERROR: API request failed during {operation} for OW Chat ID {ow_chat_id}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Request URL: {e.request.url}")
            print(f"Response Status: {e.response.status_code}")
            try:
                error_details = e.response.json(); print(f"Response JSON: {error_details}")
            except json.JSONDecodeError:
                print(f"Response Text: {e.response.text[:500]}...")
        success = False
    except Exception as e:
        print(f"An unexpected error occurred during {operation} for OW Chat ID {ow_chat_id}: {e}")
        traceback.print_exc()
        success = False

    return success


# --- Main Sync Logic ---
def main():
    """Main function to orchestrate the synchronization process."""
    start_time = datetime.now(timezone.utc)
    print(f"--- Starting sync run: {start_time.isoformat()} ---")
    print(f"--- Mode: Title Prefix ID Tracking ---")

    # Load the last known sync timestamp (as ISO string)
    state = load_sync_state()
    last_sync_ts_iso = state.get('last_sync_timestamp', INITIAL_STATE_ISO)

    # Convert loaded ISO timestamp to Epoch Seconds for DB Query
    last_sync_ts_epoch = EPOCH_ZERO_SECONDS
    try:
        dt_obj = datetime.fromisoformat(last_sync_ts_iso.replace('Z', '+00:00'))
        last_sync_ts_epoch = int(dt_obj.timestamp())
        print(f"Syncing chats updated after: {last_sync_ts_iso} (Epoch: {last_sync_ts_epoch})")
    except ValueError:
        print(f"Error parsing timestamp '{last_sync_ts_iso}' from state file. Defaulting to Epoch 0.")
        print(f"Syncing chats updated after: {INITIAL_STATE_ISO} (Epoch: {last_sync_ts_epoch})")

    # Establish DB connection
    db_conn = get_db_connection()
    if not db_conn: print("Halting run due to DB connection failure."); return

    # Find or create the target Karakeep list
    karakeep_list_id = find_or_create_karakeep_list(TARGET_LIST_NAME)
    if not karakeep_list_id:
        print("Halting run: Could not find or create target Karakeep list.")
        if db_conn: db_conn.close(); return

    # Build the map of existing Karakeep bookmarks by title prefix
    karakeep_item_map = get_karakeep_item_map_by_title(karakeep_list_id)

    # --- Query OpenWebUI Database ---
    current_run_max_timestamp_epoch = last_sync_ts_epoch
    chats_processed_count = 0
    chats_synced_successfully = 0
    chats_to_process = []

    try:
        # Select chat JSON blob and use epoch comparison
        if OPENWEBUI_DB_TYPE == 'postgres':
             try: import psycopg2.extras
             except ImportError: print("psycopg2 not installed!"); return None
             cursor = db_conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
             # Ensure 'chat' table and column names are correct for your PG setup
             query = "SELECT id, title, created_at, updated_at, chat FROM public.chat WHERE updated_at > %s ORDER BY updated_at ASC"
             params = (last_sync_ts_epoch,)
        else: # SQLite
            cursor = db_conn.cursor() # row_factory set on connection
            # Use lowercase table name 'chat', select 'chat' column
            query = "SELECT id, title, created_at, updated_at, chat FROM chat WHERE updated_at > ? ORDER BY updated_at ASC"
            params = (last_sync_ts_epoch,) # Use epoch integer parameter

        print(f"Executing DB query: {query} with params: {params}")
        start_db_query = time.time()
        cursor.execute(query, params)
        chats_to_process = cursor.fetchall() # Fetch all matching chats
        db_query_duration = time.time() - start_db_query
        print(f"Found {len(chats_to_process)} chats in OpenWebUI updated since last sync (Query took {db_query_duration:.2f}s).")
        cursor.close()

    except (sqlite3.Error, NameError if OPENWEBUI_DB_TYPE == 'postgres' else sqlite3.Error) as db_e:
         if 'psycopg2' in str(db_e) and OPENWEBUI_DB_TYPE == 'postgres': print(f"DB Error (PG): {db_e}")
         else: print(f"DB Error (SQLite): {db_e}")
         print("Check table/column names ('chat', 'updated_at', 'chat' JSON column) and DB connection.")
         chats_to_process = []
    except Exception as e:
        print(f"An unexpected error occurred during database query: {e}")
        traceback.print_exc()
        chats_to_process = []
    # --- End Database Query ---

    # --- Process Chats ---
    if chats_to_process:
        print(f"\n--- Processing {len(chats_to_process)} updated chats ---")
        for chat_row in chats_to_process:
            chats_processed_count += 1
            ow_chat_id_str = str(chat_row['id'])
            chat_updated_at_epoch = chat_row['updated_at'] # Already epoch integer

            try: log_ts_iso = datetime.fromtimestamp(chat_updated_at_epoch, timezone.utc).isoformat()
            except: log_ts_iso = f"Epoch: {chat_updated_at_epoch}"
            print(f"\n[{chats_processed_count}/{len(chats_to_process)}] Processing OW Chat ID: {ow_chat_id_str} (Updated: {log_ts_iso})")

            # Parse JSON from 'chat' column and format messages
            formatted_text = "[Conversation JSON parsing failed or no messages found]"
            messages_list = []
            try:
                chat_json_string = chat_row['chat']
                if chat_json_string:
                    chat_data_json = json.loads(chat_json_string)
                    messages_list = chat_data_json.get('messages', []) # Extract message list
                    if messages_list:
                        formatted_text = format_conversation(messages_list) # Format them
                    else:
                        print(f"Warning: 'messages' key not found or empty in JSON for chat {ow_chat_id_str}.")
                        formatted_text = "[No messages found in chat JSON data]"
                else:
                    print(f"Warning: 'chat' column is empty or null for chat {ow_chat_id_str}.")
                    formatted_text = "[Chat JSON data missing in source database]"
            except json.JSONDecodeError as json_e:
                print(f"ERROR: Failed to parse JSON from 'chat' column for OW Chat ID {ow_chat_id_str}: {json_e}")
                formatted_text = f"[ERROR: Invalid JSON structure in source database - {json_e}]"
            except Exception as e:
                print(f"ERROR: Unexpected error processing JSON/messages for OW Chat ID {ow_chat_id_str}: {e}")
                traceback.print_exc()
                formatted_text = "[ERROR: Unexpected error processing chat data]"

            # Check if this chat ID already exists in Karakeep
            existing_kk_id = karakeep_item_map.get(ow_chat_id_str)

            # Perform the Create or Update operation in Karakeep
            sync_success = sync_or_update_chat_in_karakeep(
                chat_row, formatted_text, karakeep_list_id, existing_kk_id # Pass formatted text
            )

            if sync_success:
                chats_synced_successfully += 1
                # If successful, update the latest timestamp seen *in this run*
                if chat_updated_at_epoch > current_run_max_timestamp_epoch:
                    current_run_max_timestamp_epoch = chat_updated_at_epoch
            else:
                print(f"Failed to sync OW Chat ID {ow_chat_id_str} to Karakeep. Check logs above.")

            time.sleep(0.1) # Optional short delay

    else:
        print("No chats found requiring update.")
    # --- End Processing Chats ---


    # --- Finalize Run & Save State ---
    print("\n--- Sync Run Summary ---")
    # Only update the state file if the timestamp actually advanced
    if current_run_max_timestamp_epoch > last_sync_ts_epoch:
        try:
            # Convert the latest epoch timestamp back to ISO string for saving
            new_sync_ts_iso = datetime.fromtimestamp(current_run_max_timestamp_epoch, timezone.utc)\
                                       .isoformat(timespec='milliseconds').replace('+00:00', 'Z')
            state['last_sync_timestamp'] = new_sync_ts_iso
            save_sync_state(state)
        except Exception as e:
            print(f"ERROR: Could not convert epoch {current_run_max_timestamp_epoch} to ISO timestamp for saving state: {e}")
            print("State file not updated.")
    elif chats_processed_count > 0:
         # print(f"Timestamp did not advance (Current Max Epoch: {current_run_max_timestamp_epoch}, Last Sync Epoch: {last_sync_ts_epoch}). State file unchanged.")
         pass # No need to log if nothing synced
    else:
         # print("No relevant chats processed. State file unchanged.")
         pass # No need to log if nothing processed

    # Close DB connection cleanly
    if db_conn:
        try: db_conn.close(); print("Database connection closed.")
        except Exception as e: print(f"Warning: Error closing database connection: {e}")

    end_time = datetime.now(timezone.utc)
    duration = end_time - start_time
    print(f"Run finished at: {end_time.isoformat()}")
    print(f"Total Duration: {duration}")
    print(f"Chats Found in DB Query: {len(chats_to_process)}")
    print(f"Chats Processed Attempted: {chats_processed_count}")
    print(f"Chats Synced/Updated Successfully to Karakeep: {chats_synced_successfully}")
    print("--- End of Run ---")


# --- Script Entry Point ---
if __name__ == "__main__":
    # Check for required libraries
    try:
        import requests
    except ImportError:
        print("ERROR: 'requests' library not found. Please install it: pip install requests")
        exit(1)
    if OPENWEBUI_DB_TYPE == 'postgres':
         try:
             import psycopg2
         except ImportError:
             print("ERROR: 'psycopg2-binary' library not found but needed for PostgreSQL.")
             print("Please install it: pip install psycopg2-binary")
             exit(1)

    # Check essential config values
    if not KARAKEEP_API_KEY or KARAKEEP_API_KEY.startswith('ak1_...'):
        print("ERROR: Karakeep API Key (KARAKEEP_API_KEY) is not configured. Please edit the variable.")
        exit(1)
    if not KARAKEEP_API_URL:
        print("ERROR: Karakeep API URL (KARAKEEP_API_URL) is not configured.")
        exit(1)
    if OPENWEBUI_DB_TYPE == 'sqlite' and (not OPENWEBUI_DB_PATH or OPENWEBUI_DB_PATH.endswith('EXAMPLE PATH')):
        print("ERROR: SQLite DB Path (OPENWEBUI_DB_PATH) is not configured or is set to the example path. Please edit the variable.")
        print("Ensure this script has read access to the database file.")
        exit(1)
    # Add checks for PG_CONFIG if needed

    # Call the main function to start the sync process
    main()