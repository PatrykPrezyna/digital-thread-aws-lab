#!/usr/bin/env bash
# Create (or update) the Digital Thread lab AWS resources.
#
# Prerequisites: aws CLI v2, credentials for an account you are allowed to
# mutate, python3, and (optional) gettext's envsubst. The script is safe to
# re-run: existing roles, the bucket, the stream, and the function are updated
# rather than duplicated.
#
# Usage:
#   cp infra/config.env.example infra/config.env   # then edit
#   ./infra/setup.sh
#
# Placeholders in iam/*.json (${ACCOUNT_ID}, ${BUCKET}, ${REGION}, …) are
# substituted before any IAM call.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
IAM_DIR="${ROOT_DIR}/iam"
LAMBDA_SRC="${ROOT_DIR}/lambda/fault_alert.py"

if [[ -f "${SCRIPT_DIR}/config.env" ]]; then
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/config.env"
fi

# ---------------------------------------------------------------------------
# Configuration (env vars win; sensible defaults otherwise)
# ---------------------------------------------------------------------------
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
PROJECT_NAME="${PROJECT_NAME:-dtlab}"
NOTIFY_EMAIL="${NOTIFY_EMAIL:-}"
MONTHLY_BUDGET_USD="${MONTHLY_BUDGET_USD:-5}"
ENABLE_REDSHIFT="${ENABLE_REDSHIFT:-0}"

if ! command -v aws >/dev/null 2>&1; then
  echo "error: the AWS CLI is not on PATH" >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 is required to render IAM policies and zip the Lambda" >&2
  exit 1
fi

ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
BUCKET="${BUCKET:-${PROJECT_NAME}-telemetry-${ACCOUNT_ID}}"
FIREHOSE_STREAM_NAME="${FIREHOSE_STREAM_NAME:-${PROJECT_NAME}-telemetry}"
SNS_TOPIC_NAME="${SNS_TOPIC_NAME:-${PROJECT_NAME}-fault-alerts}"
LAMBDA_FUNCTION_NAME="${LAMBDA_FUNCTION_NAME:-${PROJECT_NAME}-fault-alert}"
GLUE_DATABASE="${GLUE_DATABASE:-${PROJECT_NAME}_telemetry}"
GLUE_CRAWLER_NAME="${GLUE_CRAWLER_NAME:-${PROJECT_NAME}-telemetry-crawler}"
S3_PREFIX="${S3_PREFIX:-telemetry/}"
S3_ERROR_PREFIX="${S3_ERROR_PREFIX:-errors/}"
BUDGET_NAME="${BUDGET_NAME:-${PROJECT_NAME}-monthly}"
FIREHOSE_ROLE_NAME="${FIREHOSE_ROLE_NAME:-${PROJECT_NAME}-firehose}"
LAMBDA_ROLE_NAME="${LAMBDA_ROLE_NAME:-${PROJECT_NAME}-lambda}"
GLUE_ROLE_NAME="${GLUE_ROLE_NAME:-${PROJECT_NAME}-glue}"
REDSHIFT_ROLE_NAME="${REDSHIFT_ROLE_NAME:-${PROJECT_NAME}-redshift}"
SIMULATOR_POLICY_NAME="${SIMULATOR_POLICY_NAME:-${PROJECT_NAME}-simulator}"
# Export every placeholder used in iam/*.json so envsubst / the Python
# fallback can see them. REGION in the policies is called REGION (not AWS_REGION).
export ACCOUNT_ID BUCKET REGION S3_PREFIX S3_ERROR_PREFIX
export SNS_TOPIC_NAME LAMBDA_FUNCTION_NAME GLUE_DATABASE FIREHOSE_STREAM_NAME

# The single quotes are intentional: envsubst should receive the ${VAR} tokens unexpanded.
# shellcheck disable=SC2016
RENDER_VARS='${ACCOUNT_ID} ${BUCKET} ${REGION} ${S3_PREFIX} ${S3_ERROR_PREFIX} ${SNS_TOPIC_NAME} ${LAMBDA_FUNCTION_NAME} ${GLUE_DATABASE} ${FIREHOSE_STREAM_NAME}'

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/dtlab-setup.XXXXXX")"
cleanup() { rm -rf "${WORKDIR}"; }
trap cleanup EXIT

echo "Digital Thread lab setup"
echo "  account:  ${ACCOUNT_ID}"
echo "  region:   ${REGION}"
echo "  bucket:   ${BUCKET}"
echo "  stream:   ${FIREHOSE_STREAM_NAME}"
echo "  project:  ${PROJECT_NAME}"
echo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
render_template() {
  # Substitute ${ACCOUNT_ID}-style placeholders. Prefer envsubst (gettext);
  # fall back to Python so a stock macOS / Windows-AWS-CLI image still works.
  local src="$1" dest="$2"
  if command -v envsubst >/dev/null 2>&1; then
    envsubst "${RENDER_VARS}" < "${src}" > "${dest}"
  else
    python3 - "${src}" "${dest}" << 'PY'
import os, re, sys
src, dest = sys.argv[1], sys.argv[2]
pat = re.compile(r"\$\{([A-Z0-9_]+)\}")
text = open(src, encoding="utf-8").read()
missing = sorted({m for m in pat.findall(text) if m not in os.environ})
if missing:
    raise SystemExit(f"missing placeholder values: {', '.join(missing)}")
open(dest, "w", encoding="utf-8").write(pat.sub(lambda m: os.environ[m.group(1)], text))
PY
  fi
}

ensure_role() {
  local name="$1" trust_src="$2"
  local trust_rendered="${WORKDIR}/${name}-trust.json"
  render_template "${trust_src}" "${trust_rendered}"
  if aws iam get-role --role-name "${name}" >/dev/null 2>&1; then
    aws iam update-assume-role-policy --role-name "${name}" --policy-document "file://${trust_rendered}" >/dev/null
    echo "  updated trust policy on role ${name}"
  else
    aws iam create-role \
      --role-name "${name}" \
      --assume-role-policy-document "file://${trust_rendered}" \
      --tags Key=Project,Value="${PROJECT_NAME}" Key=Purpose,Value=digital-thread-lab \
      >/dev/null
    echo "  created role ${name}"
  fi
}

put_inline_policy() {
  local role="$1" policy_name="$2" src="$3"
  local rendered="${WORKDIR}/${policy_name}.json"
  render_template "${src}" "${rendered}"
  aws iam put-role-policy --role-name "${role}" --policy-name "${policy_name}" --policy-document "file://${rendered}"
  echo "  put inline policy ${policy_name} on ${role}"
}

role_arn() {
  aws iam get-role --role-name "$1" --query 'Role.Arn' --output text
}

# ---------------------------------------------------------------------------
# 1. S3 bucket (private, tagged, used only for this lab)
# ---------------------------------------------------------------------------
echo ">> S3 bucket"
if aws s3api head-bucket --bucket "${BUCKET}" --region "${REGION}" 2>/dev/null; then
  echo "  bucket already exists: ${BUCKET}"
else
  if [[ "${REGION}" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "${BUCKET}" --region "${REGION}" >/dev/null
  else
    aws s3api create-bucket --bucket "${BUCKET}" --region "${REGION}" \
      --create-bucket-configuration LocationConstraint="${REGION}" >/dev/null
  fi
  echo "  created bucket ${BUCKET}"
fi
aws s3api put-public-access-block --bucket "${BUCKET}" --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
aws s3api put-bucket-tagging --bucket "${BUCKET}" --tagging \
  "TagSet=[{Key=Project,Value=${PROJECT_NAME}},{Key=Purpose,Value=digital-thread-lab}]"
aws s3api put-bucket-versioning --bucket "${BUCKET}" --versioning-configuration Status=Enabled >/dev/null || true

# ---------------------------------------------------------------------------
# 2. IAM roles (least privilege, one role per service)
# ---------------------------------------------------------------------------
echo ">> IAM roles"
ensure_role "${FIREHOSE_ROLE_NAME}" "${IAM_DIR}/firehose-trust-policy.json"
put_inline_policy "${FIREHOSE_ROLE_NAME}" "${PROJECT_NAME}-firehose-s3" "${IAM_DIR}/firehose-permissions.json"

ensure_role "${LAMBDA_ROLE_NAME}" "${IAM_DIR}/lambda-trust-policy.json"
put_inline_policy "${LAMBDA_ROLE_NAME}" "${PROJECT_NAME}-lambda-access" "${IAM_DIR}/lambda-permissions.json"

ensure_role "${GLUE_ROLE_NAME}" "${IAM_DIR}/glue-trust-policy.json"
put_inline_policy "${GLUE_ROLE_NAME}" "${PROJECT_NAME}-glue-access" "${IAM_DIR}/glue-permissions.json"

ensure_role "${REDSHIFT_ROLE_NAME}" "${IAM_DIR}/redshift-trust-policy.json"
put_inline_policy "${REDSHIFT_ROLE_NAME}" "${PROJECT_NAME}-spectrum-access" "${IAM_DIR}/redshift-permissions.json"

# Managed policy students can attach to their own IAM user / lab role.
render_template "${IAM_DIR}/simulator-permissions.json" "${WORKDIR}/simulator-permissions.json"
if aws iam get-policy --policy-arn "arn:aws:iam::${ACCOUNT_ID}:policy/${SIMULATOR_POLICY_NAME}" >/dev/null 2>&1; then
  echo "  simulator policy ${SIMULATOR_POLICY_NAME} already exists (creating a new version)"
  aws iam create-policy-version \
    --policy-arn "arn:aws:iam::${ACCOUNT_ID}:policy/${SIMULATOR_POLICY_NAME}" \
    --policy-document "file://${WORKDIR}/simulator-permissions.json" \
    --set-as-default >/dev/null || echo "  warning: could not add a new policy version (the 5-version limit may be full)"
else
  aws iam create-policy \
    --policy-name "${SIMULATOR_POLICY_NAME}" \
    --policy-document "file://${WORKDIR}/simulator-permissions.json" \
    --tags Key=Project,Value="${PROJECT_NAME}" Key=Purpose,Value=digital-thread-lab \
    >/dev/null
  echo "  created managed policy ${SIMULATOR_POLICY_NAME}"
  echo "  attach it to the IAM user or role that will run the simulator:"
  echo "    aws iam attach-user-policy --user-name <you> --policy-arn arn:aws:iam::${ACCOUNT_ID}:policy/${SIMULATOR_POLICY_NAME}"
fi

# IAM is eventually consistent; Firehose creation fails if it cannot assume the role yet.
echo "  waiting 12s for IAM role propagation…"
sleep 12

# ---------------------------------------------------------------------------
# 3. Kinesis Data Firehose → S3 (60 s / 1 MiB, date-partitioned prefix)
# ---------------------------------------------------------------------------
echo ">> Firehose delivery stream"
FIREHOSE_ROLE_ARN="$(role_arn "${FIREHOSE_ROLE_NAME}")"
cat > "${WORKDIR}/firehose-s3.json" << JSON
{
  "RoleARN": "${FIREHOSE_ROLE_ARN}",
  "BucketARN": "arn:aws:s3:::${BUCKET}",
  "Prefix": "${S3_PREFIX}!{timestamp:yyyy/MM/dd/HH}/",
  "ErrorOutputPrefix": "${S3_ERROR_PREFIX}!{timestamp:yyyy/MM/dd/HH}/!{firehose:error-output-type}/",
  "BufferingHints": {
    "IntervalInSeconds": 60,
    "SizeInMBs": 1
  },
  "CompressionFormat": "UNCOMPRESSED",
  "S3BackupMode": "Disabled",
  "CloudWatchLoggingOptions": {
    "Enabled": false
  },
  "ProcessingConfiguration": {
    "Enabled": false
  }
}
JSON

if aws firehose describe-delivery-stream --delivery-stream-name "${FIREHOSE_STREAM_NAME}" --region "${REGION}" >/dev/null 2>&1; then
  aws firehose update-destination \
    --delivery-stream-name "${FIREHOSE_STREAM_NAME}" \
    --current-delivery-stream-version-id "$(aws firehose describe-delivery-stream --delivery-stream-name "${FIREHOSE_STREAM_NAME}" --region "${REGION}" --query 'DeliveryStreamDescription.VersionId' --output text)" \
    --destination-id "$(aws firehose describe-delivery-stream --delivery-stream-name "${FIREHOSE_STREAM_NAME}" --region "${REGION}" --query 'DeliveryStreamDescription.Destinations[0].DestinationId' --output text)" \
    --extended-s3-destination-update "file://${WORKDIR}/firehose-s3.json" \
    --region "${REGION}" >/dev/null
  echo "  updated stream ${FIREHOSE_STREAM_NAME}"
else
  aws firehose create-delivery-stream \
    --delivery-stream-name "${FIREHOSE_STREAM_NAME}" \
    --delivery-stream-type DirectPut \
    --extended-s3-destination-configuration "file://${WORKDIR}/firehose-s3.json" \
    --tags Project="${PROJECT_NAME}",Purpose=digital-thread-lab \
    --region "${REGION}" >/dev/null
  echo "  created stream ${FIREHOSE_STREAM_NAME}"
fi

# ---------------------------------------------------------------------------
# 4. SNS topic + email subscription
# ---------------------------------------------------------------------------
echo ">> SNS"
if [[ -z "${NOTIFY_EMAIL}" ]]; then
  echo "  warning: NOTIFY_EMAIL is empty; creating the topic but no subscription"
fi
TOPIC_ARN="$(aws sns create-topic --name "${SNS_TOPIC_NAME}" --region "${REGION}" \
  --tags Key=Project,Value="${PROJECT_NAME}" Key=Purpose,Value=digital-thread-lab \
  --query TopicArn --output text)"
echo "  topic ${TOPIC_ARN}"
if [[ -n "${NOTIFY_EMAIL}" ]]; then
  # create-topic is idempotent; subscribe is not. Skip if this email is already subscribed.
  existing="$(aws sns list-subscriptions-by-topic --topic-arn "${TOPIC_ARN}" --region "${REGION}" \
    --query "Subscriptions[?Endpoint=='${NOTIFY_EMAIL}'].SubscriptionArn" --output text || true)"
  if [[ -z "${existing}" ]]; then
    aws sns subscribe --topic-arn "${TOPIC_ARN}" --protocol email --notification-endpoint "${NOTIFY_EMAIL}" --region "${REGION}" >/dev/null
    echo "  subscribed ${NOTIFY_EMAIL} — confirm the email AWS just sent or you will not get alerts"
  else
    echo "  ${NOTIFY_EMAIL} is already subscribed (${existing})"
  fi
fi

# ---------------------------------------------------------------------------
# 5. Lambda (fault alerts) + S3 ObjectCreated notification
# ---------------------------------------------------------------------------
echo ">> Lambda"
python3 - "${LAMBDA_SRC}" "${WORKDIR}/fault_alert.zip" << 'PY'
import sys, zipfile
src, dest = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    zf.write(src, arcname="fault_alert.py")
PY

LAMBDA_ROLE_ARN="$(role_arn "${LAMBDA_ROLE_NAME}")"
if aws lambda get-function --function-name "${LAMBDA_FUNCTION_NAME}" --region "${REGION}" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "${LAMBDA_FUNCTION_NAME}" \
    --zip-file "fileb://${WORKDIR}/fault_alert.zip" --region "${REGION}" >/dev/null
  aws lambda update-function-configuration --function-name "${LAMBDA_FUNCTION_NAME}" \
    --role "${LAMBDA_ROLE_ARN}" --handler fault_alert.handler --runtime python3.12 \
    --timeout 30 --memory-size 128 \
    --environment "Variables={SNS_TOPIC_ARN=${TOPIC_ARN}}" \
    --region "${REGION}" >/dev/null
  echo "  updated function ${LAMBDA_FUNCTION_NAME}"
else
  aws lambda create-function --function-name "${LAMBDA_FUNCTION_NAME}" \
    --role "${LAMBDA_ROLE_ARN}" \
    --handler fault_alert.handler \
    --runtime python3.12 \
    --timeout 30 --memory-size 128 \
    --zip-file "fileb://${WORKDIR}/fault_alert.zip" \
    --environment "Variables={SNS_TOPIC_ARN=${TOPIC_ARN}}" \
    --tags "Project=${PROJECT_NAME},Purpose=digital-thread-lab" \
    --region "${REGION}" >/dev/null
  echo "  created function ${LAMBDA_FUNCTION_NAME}"
fi

LAMBDA_ARN="$(aws lambda get-function --function-name "${LAMBDA_FUNCTION_NAME}" --region "${REGION}" --query 'Configuration.FunctionArn' --output text)"

# Let S3 invoke the function. The statement id is stable so re-runs do not stack permissions.
aws lambda add-permission \
  --function-name "${LAMBDA_FUNCTION_NAME}" \
  --statement-id "${PROJECT_NAME}-s3-invoke" \
  --action lambda:InvokeFunction \
  --principal s3.amazonaws.com \
  --source-arn "arn:aws:s3:::${BUCKET}" \
  --source-account "${ACCOUNT_ID}" \
  --region "${REGION}" >/dev/null 2>&1 || true

# put-bucket-notification-configuration REPLACES the whole notification
# document. This lab bucket should not be shared with other projects.
cat > "${WORKDIR}/s3-notification.json" << JSON
{
  "LambdaFunctionConfigurations": [
    {
      "Id": "${PROJECT_NAME}-object-created",
      "LambdaFunctionArn": "${LAMBDA_ARN}",
      "Events": ["s3:ObjectCreated:*"],
      "Filter": {
        "Key": {
          "FilterRules": [
            {"Name": "prefix", "Value": "${S3_PREFIX}"}
          ]
        }
      }
    }
  ]
}
JSON
aws s3api put-bucket-notification-configuration --bucket "${BUCKET}" \
  --notification-configuration "file://${WORKDIR}/s3-notification.json"
echo "  S3 ObjectCreated on ${S3_PREFIX}* → ${LAMBDA_FUNCTION_NAME}"

# ---------------------------------------------------------------------------
# 6. Glue database + crawler
# ---------------------------------------------------------------------------
echo ">> Glue"
if aws glue get-database --name "${GLUE_DATABASE}" --region "${REGION}" >/dev/null 2>&1; then
  echo "  database ${GLUE_DATABASE} already exists"
else
  aws glue create-database --region "${REGION}" --database-input \
    "Name=${GLUE_DATABASE},Description=Digital Thread lab 5-axis packing-arm telemetry,Tags={Project=${PROJECT_NAME},Purpose=digital-thread-lab}" \
    >/dev/null || aws glue create-database --region "${REGION}" --database-input "Name=${GLUE_DATABASE},Description=Digital Thread lab 5-axis packing-arm telemetry" >/dev/null
  echo "  created database ${GLUE_DATABASE}"
fi

GLUE_ROLE_ARN="$(role_arn "${GLUE_ROLE_NAME}")"
CRAWLER_TARGETS="{\"S3Targets\":[{\"Path\":\"s3://${BUCKET}/${S3_PREFIX}\"}]}"
if aws glue get-crawler --name "${GLUE_CRAWLER_NAME}" --region "${REGION}" >/dev/null 2>&1; then
  aws glue update-crawler --name "${GLUE_CRAWLER_NAME}" --role "${GLUE_ROLE_ARN}" \
    --database-name "${GLUE_DATABASE}" --targets "${CRAWLER_TARGETS}" --region "${REGION}" >/dev/null
  echo "  updated crawler ${GLUE_CRAWLER_NAME}"
else
  aws glue create-crawler --name "${GLUE_CRAWLER_NAME}" --role "${GLUE_ROLE_ARN}" \
    --database-name "${GLUE_DATABASE}" --targets "${CRAWLER_TARGETS}" \
    --schema-change-policy "UpdateBehavior=UPDATE_IN_DATABASE,DeleteBehavior=LOG" \
    --tags "Project=${PROJECT_NAME},Purpose=digital-thread-lab" \
    --region "${REGION}" >/dev/null
  echo "  created crawler ${GLUE_CRAWLER_NAME}"
fi
echo "  after the simulator has landed files in S3, start the crawler:"
echo "    aws glue start-crawler --name ${GLUE_CRAWLER_NAME} --region ${REGION}"

# ---------------------------------------------------------------------------
# 7. Budget alarm (best-effort: some student accounts cannot create budgets)
# ---------------------------------------------------------------------------
echo ">> Budget alarm (\$${MONTHLY_BUDGET_USD}/month)"
cat > "${WORKDIR}/budget.json" << JSON
{
  "BudgetName": "${BUDGET_NAME}",
  "BudgetLimit": {"Amount": "${MONTHLY_BUDGET_USD}", "Unit": "USD"},
  "TimeUnit": "MONTHLY",
  "BudgetType": "COST"
}
JSON

# Notifications go to the same inbox as fault alerts when NOTIFY_EMAIL is set.
if [[ -n "${NOTIFY_EMAIL}" ]]; then
  cat > "${WORKDIR}/budget-notifications.json" << JSON
[
  {
    "Notification": {
      "NotificationType": "FORECASTED",
      "ComparisonOperator": "GREATER_THAN",
      "Threshold": 50,
      "ThresholdType": "PERCENTAGE"
    },
    "Subscribers": [{"SubscriptionType": "EMAIL", "Address": "${NOTIFY_EMAIL}"}]
  },
  {
    "Notification": {
      "NotificationType": "ACTUAL",
      "ComparisonOperator": "GREATER_THAN",
      "Threshold": 80,
      "ThresholdType": "PERCENTAGE"
    },
    "Subscribers": [{"SubscriptionType": "EMAIL", "Address": "${NOTIFY_EMAIL}"}]
  },
  {
    "Notification": {
      "NotificationType": "ACTUAL",
      "ComparisonOperator": "GREATER_THAN",
      "Threshold": 100,
      "ThresholdType": "PERCENTAGE"
    },
    "Subscribers": [{"SubscriptionType": "EMAIL", "Address": "${NOTIFY_EMAIL}"}]
  }
]
JSON
  if aws budgets create-budget --account-id "${ACCOUNT_ID}" \
      --budget "file://${WORKDIR}/budget.json" \
      --notifications-with-subscribers "file://${WORKDIR}/budget-notifications.json" >/dev/null 2>"${WORKDIR}/budget.err"; then
    echo "  created budget ${BUDGET_NAME}"
  elif grep -q "AlreadyExists" "${WORKDIR}/budget.err" 2>/dev/null || grep -q "DuplicateRecord" "${WORKDIR}/budget.err" 2>/dev/null; then
    aws budgets update-budget --account-id "${ACCOUNT_ID}" --new-budget "file://${WORKDIR}/budget.json" >/dev/null 2>"${WORKDIR}/budget-update.err" || true
    echo "  budget ${BUDGET_NAME} already exists (limit updated if permitted)"
  else
    echo "  warning: could not create an AWS Budget (common on some student/IAM-limited accounts):"
    sed 's/^/    /' "${WORKDIR}/budget.err" || true
    echo "  set a billing alarm in the console, and always run infra/teardown.sh when you finish."
  fi
else
  echo "  skipped (NOTIFY_EMAIL is empty)"
fi

# ---------------------------------------------------------------------------
# 8. Optional Redshift Serverless (off by default — this is the costly piece)
# ---------------------------------------------------------------------------
if [[ "${ENABLE_REDSHIFT}" == "1" ]]; then
  echo ">> Redshift Serverless (ENABLE_REDSHIFT=1)"
  NS="${PROJECT_NAME}-ns"
  WG="${PROJECT_NAME}-wg"
  RS_ROLE_ARN="$(role_arn "${REDSHIFT_ROLE_NAME}")"
  if aws redshift-serverless get-namespace --namespace-name "${NS}" --region "${REGION}" >/dev/null 2>&1; then
    echo "  namespace ${NS} already exists"
  else
    aws redshift-serverless create-namespace --namespace-name "${NS}" \
      --iam-roles "${RS_ROLE_ARN}" --region "${REGION}" \
      --tags key=Project,value="${PROJECT_NAME}" key=Purpose,value=digital-thread-lab >/dev/null
    echo "  created namespace ${NS}"
  fi
  if aws redshift-serverless get-workgroup --workgroup-name "${WG}" --region "${REGION}" >/dev/null 2>&1; then
    echo "  workgroup ${WG} already exists"
  else
    aws redshift-serverless create-workgroup --workgroup-name "${WG}" \
      --namespace-name "${NS}" --base-capacity 8 --publicly-accessible false \
      --region "${REGION}" \
      --tags key=Project,value="${PROJECT_NAME}" key=Purpose,value=digital-thread-lab >/dev/null
    echo "  created workgroup ${WG} (base capacity 8 RPUs). Delete it as soon as the Spectrum queries succeed."
  fi
  echo "  associate the Spectrum role if you created the namespace earlier without it:"
  echo "    aws redshift-serverless update-namespace --namespace-name ${NS} --iam-roles ${RS_ROLE_ARN} --region ${REGION}"
else
  echo ">> Redshift Serverless skipped (ENABLE_REDSHIFT=${ENABLE_REDSHIFT}). Use Athena; see sql/queries.sql."
fi

echo
echo "Setup finished."
echo "Next:"
echo "  1. Confirm the SNS (and budget) email if you have not already."
echo "  2. Attach policy arn:aws:iam::${ACCOUNT_ID}:policy/${SIMULATOR_POLICY_NAME}"
echo "     to the identity that will run the simulator, unless you are using an admin role."
echo "  3. python simulator/simulate.py --stream-name ${FIREHOSE_STREAM_NAME} --region ${REGION} --duration 180"
echo "  4. Wait ~60s for Firehose to flush, then:"
echo "     aws glue start-crawler --name ${GLUE_CRAWLER_NAME} --region ${REGION}"
echo "  5. When you are done, run:  CONFIRM=yes ./infra/teardown.sh"
