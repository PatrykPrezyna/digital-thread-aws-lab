#!/usr/bin/env bash
# Delete the Digital Thread lab AWS resources created by setup.sh.
#
# This script refuses to run unless you pass CONFIRM=yes. It only touches
# resources whose names are derived from PROJECT_NAME / the config file —
# it will not blindly empty an arbitrary bucket.
#
# Usage:
#   CONFIRM=yes ./infra/teardown.sh
#   CONFIRM=yes ./infra/teardown.sh --skip-bucket   # keep the S3 data

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "${SCRIPT_DIR}/config.env" ]]; then
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/config.env"
fi

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
PROJECT_NAME="${PROJECT_NAME:-dtlab}"
ENABLE_REDSHIFT="${ENABLE_REDSHIFT:-0}"
SKIP_BUCKET=0
for arg in "$@"; do
  case "${arg}" in
    --skip-bucket) SKIP_BUCKET=1 ;;
    -h|--help)
      echo "Usage: CONFIRM=yes $0 [--skip-bucket]"
      exit 0
      ;;
    *)
      echo "unknown argument: ${arg}" >&2
      exit 2
      ;;
  esac
done

if ! command -v aws >/dev/null 2>&1; then
  echo "error: the AWS CLI is not on PATH" >&2
  exit 1
fi

ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
BUCKET="${BUCKET:-${PROJECT_NAME}-telemetry-${ACCOUNT_ID}}"
FIREHOSE_STREAM_NAME="${FIREHOSE_STREAM_NAME:-${PROJECT_NAME}-telemetry}"
SNS_TOPIC_NAME="${SNS_TOPIC_NAME:-${PROJECT_NAME}-fault-alerts}"
LAMBDA_FUNCTION_NAME="${LAMBDA_FUNCTION_NAME:-${PROJECT_NAME}-fault-alert}"
GLUE_DATABASE="${GLUE_DATABASE:-${PROJECT_NAME}_telemetry}"
GLUE_CRAWLER_NAME="${GLUE_CRAWLER_NAME:-${PROJECT_NAME}-telemetry-crawler}"
BUDGET_NAME="${BUDGET_NAME:-${PROJECT_NAME}-monthly}"
FIREHOSE_ROLE_NAME="${FIREHOSE_ROLE_NAME:-${PROJECT_NAME}-firehose}"
LAMBDA_ROLE_NAME="${LAMBDA_ROLE_NAME:-${PROJECT_NAME}-lambda}"
GLUE_ROLE_NAME="${GLUE_ROLE_NAME:-${PROJECT_NAME}-glue}"
REDSHIFT_ROLE_NAME="${REDSHIFT_ROLE_NAME:-${PROJECT_NAME}-redshift}"
SIMULATOR_POLICY_NAME="${SIMULATOR_POLICY_NAME:-${PROJECT_NAME}-simulator}"

echo "Digital Thread lab teardown"
echo "  account:  ${ACCOUNT_ID}"
echo "  region:   ${REGION}"
echo "  bucket:   ${BUCKET}"
echo "  stream:   ${FIREHOSE_STREAM_NAME}"
echo "  project:  ${PROJECT_NAME}"
echo

if [[ "${CONFIRM:-}" != "yes" ]]; then
  echo "Refusing to delete anything because CONFIRM is not 'yes'."
  echo "Re-run as:  CONFIRM=yes $0"
  exit 1
fi

# Guardrail: only delete a bucket whose name starts with the project prefix
# and (when tags are readable) is tagged Purpose=digital-thread-lab.
bucket_is_ours() {
  local name="$1"
  case "${name}" in
    "${PROJECT_NAME}"-telemetry-*) ;;
    *)
      echo "  refusing to delete bucket ${name}: name does not match ${PROJECT_NAME}-telemetry-*" >&2
      return 1
      ;;
  esac
  local purpose
  purpose="$(aws s3api get-bucket-tagging --bucket "${name}" --query "TagSet[?Key=='Purpose'].Value" --output text 2>/dev/null || true)"
  if [[ -n "${purpose}" && "${purpose}" != "digital-thread-lab" ]]; then
    echo "  refusing to delete bucket ${name}: Purpose tag is '${purpose}'" >&2
    return 1
  fi
  return 0
}

ignore() { "$@" >/dev/null 2>&1 || true; }

echo ">> S3 notifications + Lambda"
ignore aws s3api put-bucket-notification-configuration --bucket "${BUCKET}" --notification-configuration '{}'
ignore aws lambda delete-function --function-name "${LAMBDA_FUNCTION_NAME}" --region "${REGION}"
echo "  lambda ${LAMBDA_FUNCTION_NAME} deleted (if it existed)"

echo ">> Firehose"
# Streams must be in ACTIVE (or failing) state before delete, and delete is async.
if aws firehose describe-delivery-stream --delivery-stream-name "${FIREHOSE_STREAM_NAME}" --region "${REGION}" >/dev/null 2>&1; then
  ignore aws firehose delete-delivery-stream --delivery-stream-name "${FIREHOSE_STREAM_NAME}" --region "${REGION}"
  echo "  requested deletion of stream ${FIREHOSE_STREAM_NAME} (this can take a minute)"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if ! aws firehose describe-delivery-stream --delivery-stream-name "${FIREHOSE_STREAM_NAME}" --region "${REGION}" >/dev/null 2>&1; then
      break
    fi
    sleep 6
  done
else
  echo "  stream ${FIREHOSE_STREAM_NAME} not found"
fi

echo ">> Glue"
ignore aws glue stop-crawler --name "${GLUE_CRAWLER_NAME}" --region "${REGION}"
ignore aws glue delete-crawler --name "${GLUE_CRAWLER_NAME}" --region "${REGION}"
# Drop every table in the lab database, then the database itself.
if aws glue get-database --name "${GLUE_DATABASE}" --region "${REGION}" >/dev/null 2>&1; then
  tables="$(aws glue get-tables --database-name "${GLUE_DATABASE}" --region "${REGION}" --query 'TableList[].Name' --output text 2>/dev/null || true)"
  for table in ${tables}; do
    ignore aws glue delete-table --database-name "${GLUE_DATABASE}" --name "${table}" --region "${REGION}"
  done
  ignore aws glue delete-database --name "${GLUE_DATABASE}" --region "${REGION}"
  echo "  dropped database ${GLUE_DATABASE}"
else
  echo "  database ${GLUE_DATABASE} not found"
fi

echo ">> SNS"
TOPIC_ARN="arn:aws:sns:${REGION}:${ACCOUNT_ID}:${SNS_TOPIC_NAME}"
if aws sns get-topic-attributes --topic-arn "${TOPIC_ARN}" --region "${REGION}" >/dev/null 2>&1; then
  subs="$(aws sns list-subscriptions-by-topic --topic-arn "${TOPIC_ARN}" --region "${REGION}" --query 'Subscriptions[].SubscriptionArn' --output text 2>/dev/null || true)"
  for sub in ${subs}; do
    if [[ "${sub}" != "None" && "${sub}" != "PendingConfirmation" ]]; then
      ignore aws sns unsubscribe --subscription-arn "${sub}" --region "${REGION}"
    fi
  done
  ignore aws sns delete-topic --topic-arn "${TOPIC_ARN}" --region "${REGION}"
  echo "  deleted topic ${SNS_TOPIC_NAME}"
else
  echo "  topic ${SNS_TOPIC_NAME} not found"
fi

echo ">> Optional Redshift Serverless"
if [[ "${ENABLE_REDSHIFT}" == "1" ]]; then
  WG="${PROJECT_NAME}-wg"
  NS="${PROJECT_NAME}-ns"
  ignore aws redshift-serverless delete-workgroup --workgroup-name "${WG}" --region "${REGION}"
  echo "  requested workgroup deletion (wait until it is gone before the namespace can be deleted)"
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if ! aws redshift-serverless get-workgroup --workgroup-name "${WG}" --region "${REGION}" >/dev/null 2>&1; then
      break
    fi
    sleep 10
  done
  ignore aws redshift-serverless delete-namespace --namespace-name "${NS}" --region "${REGION}"
  echo "  requested namespace deletion"
else
  echo "  skipped (ENABLE_REDSHIFT=${ENABLE_REDSHIFT})"
fi

echo ">> IAM roles and simulator policy"
delete_role() {
  local name="$1"
  if ! aws iam get-role --role-name "${name}" >/dev/null 2>&1; then
    echo "  role ${name} not found"
    return 0
  fi
  local policies
  policies="$(aws iam list-role-policies --role-name "${name}" --query 'PolicyNames' --output text 2>/dev/null || true)"
  for pol in ${policies}; do
    ignore aws iam delete-role-policy --role-name "${name}" --policy-name "${pol}"
  done
  local attached
  attached="$(aws iam list-attached-role-policies --role-name "${name}" --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null || true)"
  for arn in ${attached}; do
    ignore aws iam detach-role-policy --role-name "${name}" --policy-arn "${arn}"
  done
  ignore aws iam delete-role --role-name "${name}"
  echo "  deleted role ${name}"
}
delete_role "${FIREHOSE_ROLE_NAME}"
delete_role "${LAMBDA_ROLE_NAME}"
delete_role "${GLUE_ROLE_NAME}"
delete_role "${REDSHIFT_ROLE_NAME}"

POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${SIMULATOR_POLICY_NAME}"
if aws iam get-policy --policy-arn "${POLICY_ARN}" >/dev/null 2>&1; then
  # Detach from any users/roles/groups first, then drop non-default versions.
  python3 - "${POLICY_ARN}" << 'PY' || true
import json, subprocess, sys
arn = sys.argv[1]
raw = subprocess.check_output(["aws", "iam", "list-entities-for-policy", "--policy-arn", arn, "--output", "json"], text=True)
data = json.loads(raw)
for user in data.get("PolicyUsers") or []:
    subprocess.call(["aws", "iam", "detach-user-policy", "--user-name", user["UserName"], "--policy-arn", arn])
for role in data.get("PolicyRoles") or []:
    subprocess.call(["aws", "iam", "detach-role-policy", "--role-name", role["RoleName"], "--policy-arn", arn])
for group in data.get("PolicyGroups") or []:
    subprocess.call(["aws", "iam", "detach-group-policy", "--group-name", group["GroupName"], "--policy-arn", arn])
PY
  # JMESPath uses backticks; do not let the shell expand them.
  # shellcheck disable=SC2016
  versions="$(aws iam list-policy-versions --policy-arn "${POLICY_ARN}" --query 'Versions[?IsDefaultVersion==`false`].VersionId' --output text 2>/dev/null || true)"
  for ver in ${versions}; do
    ignore aws iam delete-policy-version --policy-arn "${POLICY_ARN}" --version-id "${ver}"
  done
  ignore aws iam delete-policy --policy-arn "${POLICY_ARN}"
  echo "  deleted policy ${SIMULATOR_POLICY_NAME}"
fi

echo ">> Budget"
ignore aws budgets delete-budget --account-id "${ACCOUNT_ID}" --budget-name "${BUDGET_NAME}"
echo "  budget ${BUDGET_NAME} deleted (if it existed and you had permission)"

echo ">> CloudWatch log groups"
ignore aws logs delete-log-group --log-group-name "/aws/lambda/${LAMBDA_FUNCTION_NAME}" --region "${REGION}"

echo ">> S3 bucket"
if [[ "${SKIP_BUCKET}" == "1" ]]; then
  echo "  skipped (--skip-bucket)"
elif bucket_is_ours "${BUCKET}"; then
  # Empty current objects and all versions (versioning was enabled in setup).
  set +e
  python3 - "${BUCKET}" << 'PY'
import sys
bucket = sys.argv[1]
try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:
    sys.exit(2)
try:
    s3 = boto3.resource("s3")
    bucket_res = s3.Bucket(bucket)
    bucket_res.object_versions.delete()
    bucket_res.delete()
    print(f"  deleted bucket {bucket}")
except (BotoCoreError, ClientError) as exc:
    print(f"  warning: could not delete bucket {bucket}: {exc}", file=sys.stderr)
    sys.exit(3)
PY
  status=$?
  set -e
  if [[ "${status}" -eq 2 || "${status}" -eq 3 ]]; then
    echo "  falling back to aws s3 rm (versioned objects may remain)"
    ignore aws s3 rm "s3://${BUCKET}" --recursive
    ignore aws s3api delete-bucket --bucket "${BUCKET}"
    echo "  attempted delete of ${BUCKET}"
  fi
fi

echo
echo "Teardown finished. If a Firehose stream or Redshift workgroup was still"
echo "deleting, re-run this script in a minute to clean up whatever remains."
