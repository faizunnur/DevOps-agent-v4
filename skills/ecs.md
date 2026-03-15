# Amazon ECS (Fargate) Deployment Skill

## Overview
Deploy containerized apps on ECS Fargate using Terraform + GitHub Actions.
No EC2, no Ansible — just ECR + ECS + ALB.

## Files needed
- terraform/main.tf         — ECR repo + ECS cluster + task + service + ALB + security groups
- Dockerfile                — app container
- .github/workflows/deploy.yml  — build image → push ECR → update ECS service
- .github/workflows/destroy.yml — terraform destroy

## Terraform pattern (ECS Fargate)
```hcl
terraform {
  backend "s3" {
    key     = "PROJECT_NAME_PLACEHOLDER/terraform.tfstate"
    encrypt = true
    # bucket and region are passed via -backend-config at init time
  }
}

provider "aws" {
  region = var.aws_region
}

variable "aws_region"    { default = "us-east-1" }
variable "project_name"  {}
variable "image_tag"     { default = "latest" }

# ECR
resource "aws_ecr_repository" "app" {
  name                 = var.project_name
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}

# ECS Cluster
resource "aws_ecs_cluster" "main" {
  name = "${var.project_name}-cluster"
}

# VPC (use default)
data "aws_vpc" "default" { default = true }
data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# Security Groups
resource "aws_security_group" "alb" {
  name   = "${var.project_name}-alb-sg"
  vpc_id = data.aws_vpc.default.id
  ingress { from_port = 80  to_port = 80  protocol = "tcp" cidr_blocks = ["0.0.0.0/0"] }
  egress  { from_port = 0   to_port = 0   protocol = "-1"  cidr_blocks = ["0.0.0.0/0"] }
}

resource "aws_security_group" "ecs" {
  name   = "${var.project_name}-ecs-sg"
  vpc_id = data.aws_vpc.default.id
  ingress { from_port = 80  to_port = 80  protocol = "tcp" security_groups = [aws_security_group.alb.id] }
  egress  { from_port = 0   to_port = 0   protocol = "-1"  cidr_blocks = ["0.0.0.0/0"] }
}

# ALB
resource "aws_lb" "main" {
  name               = "${replace(var.project_name, "_", "-")}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = data.aws_subnets.default.ids
}

resource "aws_lb_target_group" "app" {
  name        = "${replace(var.project_name, "_", "-")}-tg"
  port        = 80
  protocol    = "HTTP"
  vpc_id      = data.aws_vpc.default.id
  target_type = "ip"
  health_check { path = "/" healthy_threshold = 2 }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

# IAM
resource "aws_iam_role" "ecs_task" {
  name = "${var.project_name}-ecs-task-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow" Principal = { Service = "ecs-tasks.amazonaws.com" } Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_task" {
  role       = aws_iam_role.ecs_task.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Task Definition
resource "aws_ecs_task_definition" "app" {
  family                   = var.project_name
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.ecs_task.arn
  container_definitions = jsonencode([{
    name      = var.project_name
    image     = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
    essential = true
    portMappings = [{ containerPort = 80 protocol = "tcp" }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = "/ecs/${var.project_name}"
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "ecs"
      }
    }
  }])
}

# CloudWatch log group
resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${var.project_name}"
  retention_in_days = 7
}

# ECS Service
resource "aws_ecs_service" "app" {
  name            = "${var.project_name}-service"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = true
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = var.project_name
    container_port   = 80
  }
  depends_on = [aws_lb_listener.http]
}

output "alb_url" {
  value = "http://${aws_lb.main.dns_name}"
}
```

## Pipeline pattern (ECS deploy)
```yaml
on:
  workflow_dispatch:

jobs:
  terraform:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region: ${{ secrets.AWS_REGION }}
      - uses: hashicorp/setup-terraform@v3
      - name: Terraform Init
        run: |
          terraform init \
            -backend-config="bucket=${{ secrets.TF_STATE_BUCKET }}" \
            -backend-config="region=${{ secrets.AWS_REGION }}"
        working-directory: terraform
      - name: Terraform Apply
        run: terraform apply -auto-approve \
          -var="project_name=${{ secrets.PROJECT_NAME }}" \
          -var="aws_region=${{ secrets.AWS_REGION }}"
        working-directory: terraform

  build-and-push:
    needs: terraform
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region: ${{ secrets.AWS_REGION }}
      - uses: aws-actions/amazon-ecr-login@v2
        id: login-ecr
      - name: Build and push
        run: |
          IMAGE="${{ steps.login-ecr.outputs.registry }}/${{ secrets.PROJECT_NAME }}:${{ github.sha }}"
          IMAGE_LATEST="${{ steps.login-ecr.outputs.registry }}/${{ secrets.PROJECT_NAME }}:latest"
          docker build -t $IMAGE .
          docker tag $IMAGE $IMAGE_LATEST
          docker push $IMAGE
          docker push $IMAGE_LATEST
          echo "image=$IMAGE" >> $GITHUB_OUTPUT

  deploy:
    needs: build-and-push
    runs-on: ubuntu-latest
    steps:
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region: ${{ secrets.AWS_REGION }}
      - name: Update ECS service
        run: |
          aws ecs update-service \
            --cluster ${{ secrets.PROJECT_NAME }}-cluster \
            --service ${{ secrets.PROJECT_NAME }}-service \
            --force-new-deployment

  notify:
    needs: deploy
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region: ${{ secrets.AWS_REGION }}
      - uses: hashicorp/setup-terraform@v3
      - name: Print live URL
        run: |
          terraform init -reconfigure \
            -backend-config="bucket=${{ secrets.TF_STATE_BUCKET }}" \
            -backend-config="region=${{ secrets.AWS_REGION }}"
          ALB_DNS=$(terraform output -raw alb_url)
          echo "Application is available at: $ALB_DNS"
        working-directory: terraform
```

## Key Rules
- ECS uses ALB DNS name as URL (not EC2 IP)
- No SSH keys needed — no EC2
- No Ansible — ECS pulls from ECR directly
- Always use FARGATE launch type
- Always use awsvpc network mode
- Task CPU/memory: 256/512 for small apps
- Use default VPC and subnets for simplicity
- ECR repo must exist before pipeline runs (terraform creates it)
- Secrets needed: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, PROJECT_NAME
- Output: alb_url from terraform
- notify job reads alb_url from terraform output (guaranteed to match what was deployed, full URL never masked)
- ALB DNS comes from terraform state not AWS secrets, so region is never redacted in logs

## CRITICAL: Single File Rule
- ALL terraform code goes in ONE file: `terraform/main.tf`
- NEVER create separate `outputs.tf`, `variables.tf`, or `providers.tf`
- Duplicate output names will cause `terraform init` to fail immediately