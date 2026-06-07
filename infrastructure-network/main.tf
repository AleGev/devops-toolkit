# ==========================================
# Section 1: (VPC)
# ==========================================
resource "aws_vpc" "custom_vpc" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags = { Name = "production-vpc" }
}

# Аппаратный выход в глобальную сеть
resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.custom_vpc.id
  tags = { Name = "production-igw" }
}

# ==========================================
# Section 2: Public Sector (Gateway + NAT)
# ==========================================
resource "aws_subnet" "public_subnet" {
  count = length(var.public_subnets_cidr)
  vpc_id                  = aws_vpc.custom_vpc.id
  cidr_block              = var.public_subnets_cidr[count.index]
  map_public_ip_on_launch = true
  availability_zone       = var.azs[count.index]
  tags = { Name = "subnet-${count.index}" 
  "kubernetes.io/role/elb" = "1"
  "kubernetes.io/cluster/my-cluster" = "shared" # necessary tag for ALB integration with EKS  
  }
  }

resource "aws_route_table" "public_rt" {
  vpc_id = aws_vpc.custom_vpc.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }
  tags = {Name = "public-route-table"}
}

resource "aws_route_table_association" "public_assoc" {
  count = length(var.public_subnets_cidr)
  subnet_id      = aws_subnet.public_subnet[count.index].id
  route_table_id = aws_route_table.public_rt.id
}


resource "aws_security_group" "nat_sg" {
  name        = "nat-instance-sg"
  vpc_id      = aws_vpc.custom_vpc.id

  ingress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = var.private_subnets_cidr 
  } 

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"] 
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"] 
  }
}

resource "aws_iam_role" "role" {
  name = "nat-instance-role"


  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "role_attachment" {
  role       = aws_iam_role.role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore" 
}

resource "aws_iam_instance_profile" "profile" {
    name = "nat-instance-profile"
    role = aws_iam_role.role.name
}

data "aws_ami" "amazon_linux" {
  most_recent = true
  owners      = ["amazon"]
  filter { 
    name      = "name"
    values    = ["amzn2-ami-hvm-*-x86_64-gp2"] 
    }
}

resource "aws_key_pair" "bastion_key" {
  key_name   = "bastion-key-"
  public_key = file("${path.root}/../infrastructure-app/github_actions_key.pub")
}


resource "aws_network_interface" "nat_eni" {
  subnet_id = aws_subnet.public_subnet[0].id # first public subnet for NAT
  security_groups = [ aws_security_group.nat_sg.id ]
  tags = { Name = "nat-eni"}
  source_dest_check      = false
}

resource "aws_eip" "nat_eip" {
  domain   = "vpc"
}

resource "aws_eip_association" "eip_assoc" {
  allocation_id = aws_eip.nat_eip.id
  network_interface_id = aws_network_interface.nat_eni.id
}

resource "aws_instance" "nat_instance" {
  ami                    = data.aws_ami.amazon_linux.id
  instance_type          = "t3.micro"
  #subnet_id              = aws_subnet.public_subnet.id
  #vpc_security_group_ids = [aws_security_group.nat_sg.id]
  key_name               = aws_key_pair.bastion_key.key_name
  iam_instance_profile   = aws_iam_instance_profile.profile.name


 # 1. STRICT INSTRUCTION: Destroy and recreate the server when the script changes
  user_data_replace_on_change = true

  user_data = <<-EOF
              #!/bin/bash
              # 1. Enable packet forwarding in Linux kernel (IP forwarding)

              # enable routing
              echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf

              # disable reverse path filtering (important for NAT)
              echo "net.ipv4.conf.all.rp_filter=0" >> /etc/sysctl.conf
              echo "net.ipv4.conf.default.rp_filter=0" >> /etc/sysctl.conf
              sysctl -p

              # Install routing tool (iptables)
              dnf install -y iptables

              # 2. In AL2023, iptables is available by default as a wrapper.
              # We just apply NAT masquerading rule without legacy service setup.
              iptables -t nat -A POSTROUTING -o eth0 -s ${aws_vpc.custom_vpc.cidr_block} -j MASQUERADE

              # --- NEW BLOCK: FORCE START SSM AGENT ---
              systemctl enable amazon-ssm-agent
              systemctl start amazon-ssm-agent
              EOF

              
  # Set our network interface as the primary one (eth0)
  network_interface {
    network_interface_id = aws_network_interface.nat_eni.id
    device_index = 0
  }
  tags = { Name = "nat-instance" }

}

# ==========================================
# Section 3: Private Sector (Isolation)
# ==========================================
resource "aws_subnet" "private_subnet" {
  count = length(var.private_subnets_cidr)
  vpc_id                  = aws_vpc.custom_vpc.id
  cidr_block              = var.private_subnets_cidr[count.index]
  map_public_ip_on_launch = false
  availability_zone       = var.azs[count.index]
  tags = { 
    Name = "private-subnet-${count.index}" 
    "kubernetes.io/role/internal-elb" = "1" # Tag for automatic integration with internal load balancers in EKS
    "kubernetes.io/cluster/my-cluster" = "shared" # Required tag for node association with the cluster
  }
}

resource "aws_route_table" "private_rt" {
  vpc_id = aws_vpc.custom_vpc.id
  route {
    cidr_block           = "0.0.0.0/0"
    network_interface_id = aws_network_interface.nat_eni.id
  }
  tags = { Name = "private-route-table" }
}

resource "aws_route_table_association" "private_assoc" {
  count = length(var.private_subnets_cidr)
  subnet_id      = aws_subnet.private_subnet[count.index].id
  route_table_id = aws_route_table.private_rt.id
}