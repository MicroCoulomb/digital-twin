param(
    [string]$Environment = "dev",   # dev | test | prod
    [string]$ProjectName = "twin"
)
$ErrorActionPreference = "Stop"

function Invoke-ExternalCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,

        [string[]]$Arguments = @()
    )

    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Command $($Arguments -join ' ')"
    }
}

Write-Host "Deploying $ProjectName to $Environment ..." -ForegroundColor Green

# 1. Build Lambda package
Set-Location (Split-Path $PSScriptRoot -Parent)   # project root
Write-Host "Building Lambda package..." -ForegroundColor Yellow
Set-Location backend
Invoke-ExternalCommand "uv" @("run", "deploy.py")
Set-Location ..

# 2. Terraform workspace & apply
Set-Location terraform
$awsAccountId = aws sts get-caller-identity --query Account --output text
if ($LASTEXITCODE -ne 0) { throw "Failed to resolve AWS account ID" }
$awsRegion = if ($env:DEFAULT_AWS_REGION) { $env:DEFAULT_AWS_REGION } else { "us-east-1" }
Invoke-ExternalCommand "terraform" @(
    "init",
    "-input=false",
    "-backend-config=bucket=twin-terraform-state-$awsAccountId",
    "-backend-config=key=$Environment/terraform.tfstate",
    "-backend-config=region=$awsRegion",
    "-backend-config=dynamodb_table=twin-terraform-locks",
    "-backend-config=encrypt=true"
)

if (-not (terraform workspace list | Select-String $Environment)) {
    Invoke-ExternalCommand "terraform" @("workspace", "new", $Environment)
} else {
    Invoke-ExternalCommand "terraform" @("workspace", "select", $Environment)
}

if ($Environment -eq "prod") {
    Invoke-ExternalCommand "terraform" @("apply", "-var-file=prod.tfvars", "-var=project_name=$ProjectName", "-var=environment=$Environment", "-auto-approve")
} else {
    Invoke-ExternalCommand "terraform" @("apply", "-var=project_name=$ProjectName", "-var=environment=$Environment", "-auto-approve")
}

$ApiUrl = terraform output -raw api_gateway_url
if ($LASTEXITCODE -ne 0) { throw "Failed to read terraform output: api_gateway_url" }
$FrontendBucket = terraform output -raw s3_frontend_bucket
if ($LASTEXITCODE -ne 0) { throw "Failed to read terraform output: s3_frontend_bucket" }
try {
    $CustomUrl = terraform output -raw custom_domain_url
    if ($LASTEXITCODE -ne 0) { $CustomUrl = "" }
} catch {
    $CustomUrl = ""
}

# 3. Build + deploy frontend
Set-Location ..\frontend

# Create production environment file with API URL
Write-Host "Setting API URL for production..." -ForegroundColor Yellow
"NEXT_PUBLIC_API_URL=$ApiUrl" | Out-File .env.production -Encoding utf8

Invoke-ExternalCommand "npm" @("install")
Invoke-ExternalCommand "npm" @("run", "build")
Invoke-ExternalCommand "aws" @("s3", "sync", ".\\out", "s3://$FrontendBucket/", "--delete")
Set-Location ..

# 4. Final summary
$CfUrl = terraform -chdir=terraform output -raw cloudfront_url
if ($LASTEXITCODE -ne 0) { throw "Failed to read terraform output: cloudfront_url" }
Write-Host "Deployment complete!" -ForegroundColor Green
Write-Host "CloudFront URL : $CfUrl" -ForegroundColor Cyan
if ($CustomUrl) {
    Write-Host "Custom domain  : $CustomUrl" -ForegroundColor Cyan
}
Write-Host "API Gateway    : $ApiUrl" -ForegroundColor Cyan
