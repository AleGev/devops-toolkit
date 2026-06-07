# Чтение параметров из State-файла сетевой инфраструктуры
data "terraform_remote_state" "network" {
  backend = "s3"
  config = {
    bucket = "algev-1990-tfstatelocker"
    key    = "network/terraform.tfstate" # Укажи точный ключ состояния твоей сети
    region = "eu-central-1"
  }
}

locals {
  #selected_instance_type = terraform.workspace == "prod" ? "t3.micro" : "t3.micro"
  selected_instance_type = "t3.micro"
  servers = {
    "app_node_1" = { 
      instance_type        = local.selected_instance_type
      server_name          = "${terraform.workspace}-AppNode_1"
      allowed_ingress_cidr = [data.terraform_remote_state.network.outputs.vpc_cidr_block]
    }

    #"app_node_2" = { 
      #instance_type        = local.selected_instance_type
     # server_name          = "${terraform.workspace}-AppNode_2"
      #allowed_ingress_cidr = ["0.0.0.0/0"]
   # } 
  } 
}

module "application_servers" {
  source               = "./modules/web_server"
  for_each             = local.servers

  server_name          = each.value.server_name
  instance_type        = each.value.instance_type
  allowed_ingress_cidr = each.value.allowed_ingress_cidr

  #public_key_path      = "~/.ssh/tf_deployer_key.pub"
   public_key_path      = file("${path.root}/github_actions_key.pub")


   # PASSING NEW NETWORK COORDINATES TO THE MODULE
  vpc_id               = data.terraform_remote_state.network.outputs.vpc_id
  subnet_id            = data.terraform_remote_state.network.outputs.private_subnet_ids[0]
  iam_instance_profile = data.terraform_remote_state.network.outputs.iam_instance_profile_name
  iam_role_id          = data.terraform_remote_state.network.outputs.aws_iam_role_id
  # Вызов функции чтения YAML файла
  #user_data_content    = templatefile("${path.root}/cloud-config.yaml", {})
  }

module "production_database" {
  source        = "./modules/database"
  db_identifier = "main-backend-db"
  environment   = "production"
  iam_role_id   = data.terraform_remote_state.network.outputs.aws_iam_role_id
}


# Resource for automatic generation of Inventory file
resource "local_file" "ansible_inventory" {
  content  = <<-EOF
   [webservers]
   ${module.application_servers["app_node_1"].instance_id} ansible_user=ec2-user ansible_ssh_private_key_file=~/.ssh/deploy_key ansible_ssh_common_args='-o StrictHostKeyChecking=no -o ProxyCommand="sh -c \"aws ssm start-session --target %h --document-name AWS-StartSSHSession --parameters portNumber=%p\""'
  EOF
  
  filename = "${path.module}/../ansible/inventory.ini"
}