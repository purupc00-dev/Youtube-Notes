# YouTube Notes & Content Scanner (Gmax Logger)

A portable, zero-setup AI-powered video note-taker. This tool automatically downloads transcripts from YouTube videos and uses Google's Gemini AI to extract detailed summaries, key takeaways, and short-form clip potential, saving them directly to a target Notion workspace page.

---

## ⚡ Features
* **Quick Summaries**: High-level overview of any video's core value.
* **Clip Potential Finder**: Pinpoints the best hooks and timestamps for short-form reels/TikToks.
* **Key Takeaways & Action Steps**: Structured action items from the video.
* **Notion Integration**: Automatically generates pages and updates your Notion workspace.
* **Zero-Setup / Portable**: Can be run instantly without installing Node.js globally on Windows.

---

## 🚀 How to Setup and Run

### Step 1: Extract the Runtimes
Since GitHub does not allow uploading large folders with thousands of files directly via the browser, the Node.js runtime and dependencies have been zipped:
1. Locate **`node-v24.16.0-win-x64.zip`** in this directory and **unzip/extract** it here so you have a folder named `node-v24.16.0-win-x64`.
2. Locate **`node_modules.zip`** in this directory and **unzip/extract** it here so you have a folder named `node_modules`.

### Step 2: Add Your API Keys
1. In the main folder, create a file named `.env` (you can copy the `.env.example` file and rename it).
2. Open `.env` and fill in your keys:
   ```env
   NOTION_API_KEY=your_notion_integration_token
   GEMINI_API_KEY=your_google_ai_studio_api_key
   NOTION_PAGE_ID=your_notion_target_page_id
   ```

### Step 3: Run the Scanner
* Simply double-click **`Gmax-Logger.bat`** to run the program! 
* It will use the extracted portable Node.js runtime to execute.

---

## 📦 File Structure Guide
* `log-video.js`: Core script using the Google Generative AI SDK and Notion Client.
* `node-v24.16.0-win-x64/` (from zip): Embedded portable Node.js build for Windows.
* `node_modules/` (from zip): Pre-installed project dependencies.
* `Gmax-Logger.bat`: A batch script that runs the application.
