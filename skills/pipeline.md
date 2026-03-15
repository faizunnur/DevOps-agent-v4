# GitHub Actions Pipeline Best Practices

## Triggers — NEVER USE push
Always use ONLY `workflow_dispatch` so that the bot can trigger deployments exactly when needed. Do NOT use `on: push`.
```yaml
on:
  workflow_dispatch:
```

## Node.js 24 — ALWAYS required
Every workflow MUST include this top-level `env:` block to suppress Node.js 20 deprecation warnings and ensure compatibility with Node.js 24 GitHub Actions runners:
```yaml
env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true
```
This must appear at the workflow level (not inside a job), after `on:` and before `jobs:`.
## Structure — always these jobs in order
1. provision  — terraform (skip if EC2 exists)
2. configure  — ansible
3. verify     — health check
4. notify     — print live URL

## EC2 existence check — always add before terraform
```yaml
- name: Check existing EC2
  id: check_ec2
  run: |
    EXISTING_IP=$(aws ec2 describe-instances \
      --filters "Name=tag:Project,Values=${{ secrets.PROJECT_NAME }}" \
                "Name=instance-state-name,Values=running" \
      --query "Reservations[0].Instances[0].PublicIpAddress" \
      --output text 2>/dev/null || echo "None")
    if [ "$EXISTING_IP" != "None" ] && [ "$EXISTING_IP" != "null" ] && [ -n "$EXISTING_IP" ]; then
      echo "exists=true"  >> $GITHUB_OUTPUT
      echo "ip=$EXISTING_IP" >> $GITHUB_OUTPUT
    else
      echo "exists=false" >> $GITHUB_OUTPUT
    fi
```

## Terraform steps — conditional on EC2 not existing
```yaml
- name: Terraform Init
  if: steps.check_ec2.outputs.exists != 'true'
  run: |
    terraform init -reconfigure \
      -backend-config="bucket=${{ secrets.TF_STATE_BUCKET }}" \
      -backend-config="region=${{ secrets.AWS_REGION }}" \
      -backend-config="key=${{ secrets.PROJECT_NAME }}/terraform.tfstate"
  working-directory: terraform

- name: Terraform Plan
  if: steps.check_ec2.outputs.exists != 'true'
  run: |
    terraform plan \
      -var="public_key=${{ secrets.SSH_PUBLIC_KEY }}" \
      -var="project_name=${{ secrets.PROJECT_NAME }}" \
      -var="aws_region=${{ secrets.AWS_REGION }}" \
      -out=tfplan
  working-directory: terraform

- name: Terraform Apply
  if: steps.check_ec2.outputs.exists != 'true'
  run: terraform apply -auto-approve tfplan
  working-directory: terraform
```

## Get IP — handle both existing and new EC2
```yaml
- name: Get IP
  id: get_ip
  run: |
    if [ "${{ steps.check_ec2.outputs.exists }}" == "true" ]; then
      echo "ip=${{ steps.check_ec2.outputs.ip }}" >> $GITHUB_OUTPUT
    else
      echo "ip=$(terraform output -raw public_ip)" >> $GITHUB_OUTPUT
    fi
  working-directory: terraform
```

## SSH wait — always wait before ansible
```yaml
- name: Wait for SSH
  run: |
    for i in $(seq 1 30); do
      ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
        -i /tmp/deploy_key ${{ secrets.SSH_USER || 'ubuntu' }}@${{ needs.provision.outputs.server_ip }} echo ok \
        && break || sleep 10
    done
```

## Ansible step — ALWAYS use this exact env block
NEVER set `ANSIBLE_STDOUT_CALLBACK`. Do NOT use community.general.yaml. Just set ANSIBLE_HOST_KEY_CHECKING.
```yaml
- name: Run Ansible
  run: |
    ansible-playbook \
      -i "${{ needs.provision.outputs.server_ip }}," \
      --private-key /tmp/deploy_key \
      -u ${{ secrets.SSH_USER }} \
      playbook.yml
  working-directory: ansible
  env:
    AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
    AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
    AWS_DEFAULT_REGION: ${{ secrets.AWS_REGION }}
    ANSIBLE_HOST_KEY_CHECKING: "False"
```

## Destroy job — always use `public_key` variable name, NOT `ssh_public_key`
```yaml
- name: Terraform Destroy
  run: |
    terraform destroy -auto-approve \
      -var="public_key=${{ secrets.SSH_PUBLIC_KEY }}" \
      -var="project_name=${{ secrets.PROJECT_NAME }}" \
      -var="aws_region=${{ secrets.AWS_REGION }}"
  working-directory: terraform
```
CRITICAL: The Terraform variable is always named `public_key`. Using `-var="ssh_public_key=..."` causes Terraform to prompt for the real variable interactively, hanging the CI runner forever.

## S3 bucket creation — before terraform init
Always use secrets.TF_STATE_BUCKET and secrets.AWS_REGION — never hardcode bucket name or region.
```yaml
- name: Create S3 state bucket
  if: steps.check_ec2.outputs.exists != 'true'
  run: aws s3 mb s3://${{ secrets.TF_STATE_BUCKET }} --region ${{ secrets.AWS_REGION }} 2>/dev/null || true
```

## Secrets always needed
- AWS_ACCESS_KEY_ID
- AWS_SECRET_ACCESS_KEY
- AWS_REGION
- SSH_PRIVATE_KEY
- SSH_PUBLIC_KEY
- PROJECT_NAME
- TF_STATE_BUCKET  ← account-specific S3 bucket name, never hardcode this
- SSH_USER         ← default: ubuntu

## Concurrency — prevent parallel runs
```yaml
concurrency:
  group: deploy-${{ github.repository }}
  cancel-in-progress: false
```

## Always check ALL existing resources before terraform
```yaml
- name: Check existing AWS resources
  id: check_ec2
  run: |
    PROJECT="${{ secrets.PROJECT_NAME }}"
    EXISTING_IP=$(aws ec2 describe-instances \
      --filters "Name=tag:Project,Values=$PROJECT" \
                "Name=instance-state-name,Values=running" \
      --query "Reservations[0].Instances[0].PublicIpAddress" \
      --output text 2>/dev/null || echo "None")
    KEY_EXISTS=$(aws ec2 describe-key-pairs \
      --filters "Name=key-name,Values=$PROJECT-key" \
      --query "KeyPairs[0].KeyName" --output text 2>/dev/null || echo "None")
    SG_ID=$(aws ec2 describe-security-groups \
      --filters "Name=group-name,Values=$PROJECT-sg" \
      --query "SecurityGroups[0].GroupId" --output text 2>/dev/null || echo "None")
    [ "$EXISTING_IP" != "None" ] && echo "exists=true" >> $GITHUB_OUTPUT || echo "exists=false" >> $GITHUB_OUTPUT
    echo "key_exists=$KEY_EXISTS" >> $GITHUB_OUTPUT
    echo "sg_id=$SG_ID" >> $GITHUB_OUTPUT
```

## Import existing resources before terraform plan
```yaml
- name: Import existing resources
  if: steps.check_ec2.outputs.exists != 'true'
  run: |
    KEY="${{ steps.check_ec2.outputs.key_exists }}"
    SG="${{ steps.check_ec2.outputs.sg_id }}"
    [ "$KEY" != "None" ] && terraform import -var="public_key=${{ secrets.SSH_PUBLIC_KEY }}" aws_key_pair.deployer "${{ secrets.PROJECT_NAME }}-key" 2>/dev/null || true
    [ "$SG" != "None" ] && terraform import -var="public_key=${{ secrets.SSH_PUBLIC_KEY }}" aws_security_group.sg "$SG" 2>/dev/null || true
  working-directory: terraform
```