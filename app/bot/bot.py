import os
import telebot
import threading 
import time
import logging
import boto3 
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

def write_heartbeat():
    file_path = "/app/heartbeat.txt"
    logging.info(f"Starting background heartbeat thread. File: {file_path}")
    while True:
        with open(file_path, "w") as f:
            f.write(str(time.time()))
        time.sleep(30)

# 1. ENVIRONMENT CONFIGURATION
aws_region = os.getenv("AWS_REGION", "eu-central-1")
ssm_parameter_name = os.getenv("SSM_PARAMETER_NAME")
s3_shared_bucket = os.getenv("S3_SHARED_BUCKET") # name of the S3 bucket to generate links for

if not s3_shared_bucket:
    logging.error("CRITICAL: S3_SHARED_BUCKET environment variable is missing.")
    exit(1)

bot_token = None

# 2. FETCH BOT TOKEN VIA SSM (USING IRSA)
if ssm_parameter_name:
    logging.info("AWS SSM pointer detected. Fetching Telegram token...")
    try:
        # boto3 automatically uses the Pod's Service Account token
        ssm_client = boto3.client('ssm', region_name=aws_region)
        response_token = ssm_client.get_parameter(Name=ssm_parameter_name, WithDecryption=True)
        bot_token = response_token['Parameter']['Value']
        logging.info("Token retrieved successfully.")
    except Exception as e:
        logging.error(f"CRITICAL AWS ERROR: Failed to retrieve data from SSM. {e}")
        exit(1)
else:
    bot_token = os.getenv("BOT_TOKEN")

if not bot_token:
    logging.error("CRITICAL: Telegram token not found. Terminating.")
    exit(1)

# 3. INITIALIZATION
bot = telebot.TeleBot(bot_token)
# S3 client automatically assumes the IRSA role (ssm-app-role)
s3_client = boto3.client('s3', region_name=aws_region)

MAX_EXPIRATION_MINUTES = 60 # Ограничение безопасности STS сессии

# --- APPLICATION LOGIC ---

@bot.message_handler(commands=["start", "help"])
def help_command(message):
    help_text = (
        f"Secure File Gateway Initialized.\n"
        f"Target Bucket: `{s3_shared_bucket}`\n\n"
        "Usage:\n"
        "`/link <path/to/file> <minutes>`\n\n"
        "Example:\n"
        "`/link reports/data.csv 15`\n\n"
        f"Note: Max expiration time is {MAX_EXPIRATION_MINUTES} minutes due to token security limits."
    )
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")

@bot.message_handler(commands=["link"])
def generate_link_command(message):
    user_id = str(message.chat.id)
    parts = message.text.split()
    
    # Validation: Require exact command structure
    if len(parts) != 3:
        bot.send_message(message.chat.id, "Syntax Error. Expected format: `/link <file_key> <minutes>`", parse_mode="Markdown")
        return

    object_key = parts[1]
    
    try:
        expiration_minutes = int(parts[2])
        
        if expiration_minutes <= 0 or expiration_minutes > MAX_EXPIRATION_MINUTES:
            bot.send_message(
                message.chat.id, 
                f"Error: Expiration time must be between 1 and {MAX_EXPIRATION_MINUTES} minutes."
            )
            return

        expiration_seconds = expiration_minutes * 60

        # Generate pre-signed URL using assumed IRSA credentials
        presigned_url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': s3_shared_bucket, 'Key': object_key},
            ExpiresIn=expiration_seconds
        )
        
        logging.info(f"URL generated for User {user_id}. Object: {object_key}, Exp: {expiration_minutes}m")
        
        response_msg = (
            f"✅ **Link Generated Successfully**\n\n"
            f"**File:** `{object_key}`\n"
            f"**Valid for:** {expiration_minutes} minutes\n\n"
            f"{presigned_url}"
        )
        bot.send_message(message.chat.id, response_msg, parse_mode="Markdown")

    except ValueError:
        bot.send_message(message.chat.id, "Error: The expiration time must be an integer.")
    except ClientError as e:
        logging.error(f"AWS Error generating URL: {e}")
        bot.send_message(
            message.chat.id, 
            "AWS API Error. Verify the file exists and the bot has correct IAM permissions."
        )

if __name__ == "__main__":
    heartbeat_thread = threading.Thread(target=write_heartbeat, daemon=True)
    heartbeat_thread.start()
    
    logging.info(f"Bot listening. Gateway bucket set to: {s3_shared_bucket}")
    bot.infinity_polling()