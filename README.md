# OpenWebUI to Karakeep/Hoarder Sync Script

========


Note, I am *NOT* a software developer.  I mostly vibe coded this with Gemini 2.5, along with some manual work running CURL commands to see how the API was responding.  Don't kill me.  Open to any suggestions for improvements.

==========

This Python script synchronizes chat conversations from an [OpenWebUI](https://github.com/open-webui/open-webui) instance to a [Karakeep](https://github.com/karakeep-app/karakeep) / Hoarder instance. It reads chat data directly from the OpenWebUI database and uses the Karakeep/Hoarder API (v1) to create or update bookmarks.

## Features

* **Incremental Syncing:** Uses timestamps (`updated_at`) and a state file (`sync_state_title_id.json`) to only process chats created or updated since the last successful run.
* **Database Support:** Supports both SQLite (default for OpenWebUI Docker) and PostgreSQL databases.
* **Idempotent:** Prevents duplicate entries and handles updates. It embeds the OpenWebUI chat ID into the Karakeep bookmark title (`Title [OW_ID:...]`) and checks for existing entries before creating or updating.
* **Karakeep List Management:** Automatically finds the target list in Karakeep by name or creates it if it doesn't exist.
* **Data Formatting:** Formats the chat conversation (roles, timestamps, content) into a readable Markdown format within the Karakeep bookmark's text field.
* **Error Handling:** Includes basic error handling for database connections, API requests, and file operations.

## Requirements

* Python 3.x
* `requests` library (`pip install requests`)
* `psycopg2-binary` library (only if using PostgreSQL for OpenWebUI) (`pip install psycopg2-binary`)
* Read access to the OpenWebUI database file (SQLite) or database credentials (PostgreSQL).
* Karakeep/Hoarder instance URL and a generated API Key.

## Configuration

Configuration is done by editing the variables directly within the Python script (`your_script_name.py`):

1.  **Database Configuration:**
    * Locate the `# --- Database Configuration ---` section.
    * Set `OPENWEBUI_DB_TYPE` to either `'sqlite'` or `'postgres'`.
    * **If using SQLite (Default):**
        * Uncomment the `OPENWEBUI_DB_TYPE = 'sqlite'` line.
        * **Crucially:** Update `OPENWEBUI_DB_PATH` to the **full, correct path** to your `webui.db` file.
            * *Note for Docker users:* This is the path **on the host machine** where the database volume is mounted, *not* the path inside the container (which is often `/app/backend/data/webui.db`). Example host path: `/path/to/your/docker/volumes/openwebui/data/webui.db`.
            * Ensure the script has **read permissions** for this file.
        * Leave `PG_CONFIG` commented out or as `{}`.
    * **If using PostgreSQL:**
        * Uncomment the `OPENWEBUI_DB_TYPE = 'postgres'` line.
        * Comment out the `OPENWEBUI_DB_PATH` line.
        * Fill in the `PG_CONFIG` dictionary with your PostgreSQL database name, user, password, host, and port. Replace the placeholder values.
        * Ensure the `psycopg2-binary` library is installed.

2.  **Karakeep/Hoarder Configuration:**
    * Locate the `# --- Karakeep/Hoarder Configuration ---` section.
    * Set `KARAKEEP_API_URL` to the **full URL** of your Karakeep/Hoarder API endpoint, including `/api/v1`. Example: `http://your-karakeep-ip:34300/api/v1`.
    * Set `KARAKEEP_API_KEY` to the API key you generated within your Karakeep/Hoarder settings. **Replace the example key!**
    * Set `TARGET_LIST_NAME` to the desired name of the list in Karakeep where the chats will be stored (e.g., `'OpenWebUI Chats'`). The script will create this list if it doesn't exist.

3.  **Script State & Title ID Format (Optional):**
    * `STATE_FILE`: Defines the name of the file storing the last sync timestamp. Usually no need to change.
    * `TITLE_ID_PREFIX`, `TITLE_ID_SUFFIX`: Define the format for embedding the OW_ID in the title. Changed in this version to create a suffix like `Title [OW_ID:xyz]`. Modifying these requires updating the regex logic accordingly.
    * `MAX_KARAKEEP_TITLE_LENGTH`: Maximum allowed title length in Karakeep.

## How it Works

1.  **Load State:** Reads the `last_sync_timestamp` from the `STATE_FILE`. Defaults to the beginning of the epoch if the file is missing or invalid.
2.  **Connect DB:** Establishes a connection to the configured OpenWebUI database (SQLite or PostgreSQL).
3.  **Find/Create List:** Checks if the `TARGET_LIST_NAME` exists in Karakeep via the API. Creates it if not found.
4.  **Map Existing Items:** Fetches bookmarks from the target Karakeep list (using pagination). Parses the title of each bookmark using a regular expression to find the `[OW_ID:...]` suffix and extracts the `ow_chat_id`. Creates a map `{ow_chat_id: karakeep_bookmark_id}`.
5.  **Query DB:** Queries the OpenWebUI `chat` table for rows where `updated_at` (as epoch seconds) is greater than the `last_sync_timestamp` loaded from the state file.
6.  **Process Chats:** For each new or updated chat found in the database:
    * Parses the chat history from the JSON blob in the `chat` column.
    * Formats the conversation into Markdown text.
    * Constructs the target bookmark title (Original Title + ID Suffix), handling truncation if necessary.
    * Checks the map created in step 4 to see if this `ow_chat_id` already has a corresponding Karakeep bookmark.
    * **If exists:** Sends a `PUT` request to update the existing Karakeep bookmark.
    * **If not exists:** Sends a `POST` request to create a new global bookmark, then a `PUT` request to link it to the target list.
    * Tracks the maximum `updated_at` timestamp encountered among successfully synced items in the current run.
7.  **Save State:** If any chats were successfully synced and the maximum timestamp advanced, converts the latest timestamp back to ISO format and writes it to the `STATE_FILE`.
8.  **Cleanup:** Closes the database connection and prints a summary of the run.

## Usage

1.  Ensure you have met the Requirements and completed the Configuration steps.
2.  Run the script from your terminal:
    ```bash
    python your_script_name.py
    ```
3.  You can schedule this script to run periodically (e.g., using `cron` on Linux/macOS or Task Scheduler on Windows) to keep Karakeep updated.

## Limitations

* **No Deletion Sync:** This script only creates and updates chats in Karakeep. If you delete a chat in OpenWebUI, it will **not** be automatically deleted from Karakeep by this script.
* **Error Handling:** While basic error handling is present, robust retry logic for transient network issues is not implemented.

## License

MIT License
This script is provided as-is. Please review and test it carefully before relying on it for critical data.
