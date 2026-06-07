resource "aws_cloudwatch_metric_alarm" "ec2_cpu_alarm" {
  alarm_name          = "ec2-high-cpu-utilization"
  alarm_description   = "Monitors CPU utilization for application servers"
  
  namespace           = "AWS/EC2"
  metric_name         = "CPUUtilization"
  dimensions = {
    InstanceId = module.application_servers["app_node_1"].instance_id 
  }

  period              = 120
  statistic           = "Average"
  threshold           = 80
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 2
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# --- section for RDS Free Storage Space Alarm ---
resource "aws_cloudwatch_metric_alarm" "rds_storage_alarm" {
  alarm_name          = "rds-low-storage-space"
  alarm_description   = "Monitors free storage space for PostgreSQL"
  

  # Section 1: Data Source (Same as in the Dashboard)
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  dimensions = {
    DBInstanceIdentifier = module.production_database.db_identifier
  }

  # Section 2: Aggregation
  period              = 300 # Проверяем каждые 5 минут
  statistic           = "Average"

  # Section 3: Condition (Math changes!)
  threshold           = 1000000000 # 1 GB in bytes
  comparison_operator = "LessThanThreshold" # Allarm, if less than threshold
  evaluation_periods  = 2 # enough 2 periods in a row, to avoid false positives


  # Connecting to the existing email alerting system
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# 3. Create a "Topic" (distribution node)
resource "aws_sns_topic" "alerts" {
  name = "infrastructure-alerts"
}


# 4. Create a "Subscription" (where to send the email)
resource "aws_sns_topic_subscription" "email_alert" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email # <-- Put your email here
}

data "terraform_remote_state" "storage" {
  backend = "s3"
  config = {
    bucket = "algev-1990-tfstatelocker"
    key    = "storage/terraform.tfstate"
    region = "eu-central-1"
  }
}

resource "aws_cloudwatch_dashboard" "main_dashboard" {
  dashboard_name = "Infrastructure-Overview"
  dashboard_body = jsonencode({
    widgets = [
      # --- ROW 1 (y=0): CPU ---
      {
        type = "metric", x = 0, y = 0, width = 8, height = 6
        properties = {
          view = "timeSeries", stacked = false, region = "eu-central-1"
          title   = "Bot Server CPU Utilization (%)"
          metrics = [ [ "AWS/EC2", "CPUUtilization", "InstanceId", module.application_servers["app_node_1"].instance_id ] ]
        }
      },
      {
        type = "metric", x = 8, y = 0, width = 8, height = 6
        properties = {
          view = "timeSeries", stacked = false, region = "eu-central-1"
          title   = "PostgreSQL CPU Utilization (%)"
          metrics = [ [ "AWS/RDS", "CPUUtilization", "DBInstanceIdentifier", module.production_database.db_identifier ] ]
        }
      },
      {
        type = "metric", x = 16, y = 0, width = 8, height = 6
        properties = {
          view = "timeSeries", stacked = false, region = "eu-central-1"
          title   = "Redis CPU Utilization (%)"
          metrics = [ [ "AWS/ElastiCache", "CPUUtilization", "CacheClusterId", data.terraform_remote_state.storage.outputs.redis_cluster_id ] ]
        }
      },

      # --- ROW 2 (y=6): RAM and Disk ---
      {
        type = "metric", x = 0, y = 6, width = 8, height = 6
        properties = {
          view = "timeSeries", stacked = false, region = "eu-central-1"
          title   = "PostgreSQL Free Storage Space (Bytes)"
          metrics = [ [ "AWS/RDS", "FreeStorageSpace", "DBInstanceIdentifier", module.production_database.db_identifier ] ]
        }
      },
      {
        type = "metric", x = 8, y = 6, width = 8, height = 6
        properties = {
          view = "timeSeries", stacked = false, region = "eu-central-1"
          title   = "PostgreSQL Freeable Memory (Bytes)"
          metrics = [ [ "AWS/RDS", "FreeableMemory", "DBInstanceIdentifier", module.production_database.db_identifier ] ]
        }
      },
      {
        type = "metric", x = 16, y = 6, width = 8, height = 6
        properties = {
          view = "timeSeries", stacked = false, region = "eu-central-1"
          title   = "Redis Bytes Used For Cache (Bytes)"
          metrics = [ [ "AWS/ElastiCache", "BytesUsedForCache", "CacheClusterId", data.terraform_remote_state.storage.outputs.redis_cluster_id ] ]
        }
      },

      # --- ROW 3 (y=12): Connections ---
      {
        type = "metric", x = 0, y = 12, width = 12, height = 6
        properties = {
          view = "timeSeries", stacked = false, region = "eu-central-1"
          title   = "PostgreSQL Database Connections (Count)"
          metrics = [ [ "AWS/RDS", "DatabaseConnections", "DBInstanceIdentifier", module.production_database.db_identifier ] ]
        }
      },
      {
        type = "metric", x = 12, y = 12, width = 12, height = 6
        properties = {
          view = "timeSeries", stacked = false, region = "eu-central-1"
          title   = "Redis Current Connections (Count)"
          metrics = [ [ "AWS/ElastiCache", "CurrConnections", "CacheClusterId", data.terraform_remote_state.storage.outputs.redis_cluster_id ] ]
        }
      }
    ]
  })
}