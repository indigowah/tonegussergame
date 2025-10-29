# Tone Gusser Game

## Installation

1. Create and activate a virtual environment (optional but recommended).
2. Install the project dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. (Optional) Install the Playwright browsers required for running end-to-end tests:
   ```bash
   playwright install
   ```

## Running the tone trainer

1. Export the Flask app path (only the first time in a shell):
   ```bash
   export FLASK_APP=app.py
   ```
2. Start the development server:
   ```bash
   flask run
   ```
3. Visit http://127.0.0.1:5000/ in your browser to play. Progress is tracked in `tone_stats.db`.
