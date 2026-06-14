import os
import telebot
import redis
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

# 1. RETRIEVE POINTERS FROM KUBERNETES DEPLOYMENT
aws_region = os.getenv("AWS_REGION", "eu-central-1")
ssm_parameter_name = os.getenv("SSM_PARAMETER_NAME")
ssm_redis_name = os.getenv("SSM_REDIS_NAME")

bot_token = None
actual_redis_host = None

# 2. DETERMINE DATA SOURCE (AWS OR LOCAL)
if ssm_parameter_name and ssm_redis_name:
    logging.info("AWS SSM pointers detected. Accessing cloud configuration.")
    try:
        ssm_client = boto3.client('ssm', region_name=aws_region)
        
        response_token = ssm_client.get_parameter(Name=ssm_parameter_name, WithDecryption=True)
        bot_token = response_token['Parameter']['Value']
        
        response_redis = ssm_client.get_parameter(Name=ssm_redis_name, WithDecryption=False) 
        actual_redis_host = response_redis['Parameter']['Value']

        logging.info(f"Successfully retrieved Redis address from AWS: {actual_redis_host}")
    except Exception as e:
        logging.error(f"CRITICAL AWS ERROR: Failed to retrieve data. Details: {e}")
        exit(1)
else:
    logging.warning("SSM pointers are missing. Falling back to local environment variables.")
    bot_token = os.getenv("BOT_TOKEN")
    actual_redis_host = os.getenv("REDIS_HOST", "localhost")

if not bot_token:
    logging.error("CRITICAL ERROR: Token is missing in both AWS and local environment. Terminating process.")
    exit(1)

# 3. FINAL INITIALIZATION
logging.info(f"Final bot initialization. Connecting to Redis host: {actual_redis_host}")

bot = telebot.TeleBot(bot_token)
db = redis.Redis(host=actual_redis_host, port=6379, decode_responses=True)

# Initialize S3 client for generating URLs
s3_client = boto3.client('s3', region_name=aws_region)

# --- S3 PRE-SIGNED URL LOGIC ---

@bot.message_handler(commands=["start", "config"])
def config_command(message):
    logging.info(f"Configuration request received from ID {message.chat.id}")
    bot.send_message(
        message.chat.id, 
        "Provide the AWS S3 bucket name and the URL expiration time in minutes.\n"
        "Format required: [bucket_name] [expiration_in_minutes]\n"
        "Example: company-assets-bucket 60"
    )
    bot.register_next_step_handler(message, save_configuration)

def save_configuration(message):
    user_id = str(message.chat.id)
    parts = message.text.split()
    
    # Require exactly two parameters. Exclusion logic: Prevents indexing errors and guarantees complete configuration.
    if len(parts) != 2:
        logging.warning(f"Invalid configuration format from ID {user_id}. Input: {message.text}")
        bot.send_message(
            message.chat.id, 
            "Error. Exactly two parameters are required. Execute /config to retry."
        )
        return

    bucket_name = parts[0]
    
    try:
        expiration_minutes = int(parts[1])
        # Require positive time values. Exclusion logic: Time cannot flow backward, negative seconds invalidate the AWS API request.
        if expiration_minutes <= 0:
            raise ValueError("Time parameter must be positive.")
            
        # Store configuration as a hash in Redis
        db.hset(f"user_config:{user_id}", mapping={
            "bucket": bucket_name,
            "expiration": expiration_minutes
        })
        
        logging.info(f"Configuration saved for ID {user_id}: Bucket={bucket_name}, Expiration={expiration_minutes}m")
        bot.send_message(
            message.chat.id, 
            f"Configuration applied successfully.\n"
            f"Target Bucket: {bucket_name}\n"
            f"Expiration Time: {expiration_minutes} minutes.\n\n"
            "To generate a link, send /link followed by the object key.\n"
            "Example: /link backup/database_dump.sql"
        )
    except ValueError:
        logging.warning(f"Invalid integer input for time from ID {user_id}. Input: {parts[1]}")
        bot.send_message(
            message.chat.id, 
            "Error. The expiration time must be a positive integer. Execute /config to retry."
        )

@bot.message_handler(commands=["link"])
def generate_link_command(message):
    user_id = str(message.chat.id)
    
    # Retrieve user configuration
    user_config = db.hgetall(f"user_config:{user_id}")
    
    # Verify configuration existence. Exclusion logic: The AWS API requires a target bucket; execution is impossible without it.
    if not user_config:
        bot.send_message(message.chat.id, "Configuration is missing. Execute /config to set the target bucket and time.")
        return

    # Split the message into command and object key
    parts = message.text.split(maxsplit=1)
    
    if len(parts) != 2:
        bot.send_message(message.chat.id, "Error. The object key is missing. Example: /link path/to/file.zip")
        return

    bucket_name = user_config.get("bucket")
    expiration_minutes = int(user_config.get("expiration"))
    expiration_seconds = expiration_minutes * 60
    object_key = parts[1]

    try:
        # Generate the pre-signed URL
        presigned_url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket_name, 'Key': object_key},
            ExpiresIn=expiration_seconds
        )
        
        logging.info(f"Pre-signed URL generated for ID {user_id}. Object: s3://{bucket_name}/{object_key}")
        
        bot.send_message(
            message.chat.id,
            f"URL Generation Successful.\n\n"
            f"Validity Period: {expiration_minutes} minutes.\n\n"
            f"Link:\n{presigned_url}"
        )

    except ClientError as e:
        logging.error(f"AWS ClientError during URL generation for ID {user_id}: {e}")
        bot.send_message(
            message.chat.id, 
            f"AWS API Error. Verify your AWS permissions and bucket configuration. Details: {e}"
        )

if __name__ == "__main__":
    heartbeat_thread = threading.Thread(target=write_heartbeat, daemon=True)
    heartbeat_thread.start()
    
    logging.info("Main bot loop started. Waiting for incoming messages.")
    bot.infinity_polling()