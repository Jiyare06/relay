# Running the real-Groq steps on your computer

This session's cloud sandbox can't reach api.groq.com, so these steps run on your machine.

1. Install Python 3.11+ (python.org). On Windows, tick "Add python to PATH" in the installer.
2. Unzip `relay.zip`, open a terminal in the unzipped `relay` folder, and install the two libraries:
   ```
   pip install aiohttp pandas
   ```
3. Set your key for this terminal only, so it never lands in a file you submit:
   - Windows PowerShell: `$env:GROQ_API_KEY="gsk_..."`
   - macOS/Linux: `export GROQ_API_KEY=gsk_...`
4. Run:
   ```
   python run_real.py
   ```
   It takes about 60–90 minutes, mostly waiting on rate limits, so leave the terminal open. You can run the parts separately with `python run_real.py probe`, then `eval`, then `steady`.
5. When it finishes, zip the `results` and `calibration` folders and attach them in the chat.

If it stops partway (for example, the daily limit is reached), that's useful data too. Send the folders anyway.
