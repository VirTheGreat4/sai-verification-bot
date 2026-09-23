# Package Release

## Instructions

"When the user asks to package, update, or zip the project for deployment, automatically run this exact command in the root terminal:
rm -f update.zip && zip -r update.zip bot.py ai_engine.py database.py preflight.py requirements.txt reference_images/ -x "*.git*" "*venv*" "*__pycache__*" "*tests*" "*.pytest_cache*" "*.env*"
Then confirm to the user that update.zip is ready for download."