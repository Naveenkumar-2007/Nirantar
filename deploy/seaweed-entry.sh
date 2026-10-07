#!/bin/sh
# SeaweedFS S3 with credentials from the environment (the config file is written at start, never committed).
set -e
: "${S3_ACCESS_KEY:?S3_ACCESS_KEY is required}"
: "${S3_SECRET_KEY:?S3_SECRET_KEY is required}"
umask 077
cat > /tmp/s3.json <<JSON
{"identities": [{"name": "nirantar", "credentials": [{"accessKey": "${S3_ACCESS_KEY}", "secretKey": "${S3_SECRET_KEY}"}],
  "actions": ["Admin", "Read", "Write", "List", "Tagging"]}]}
JSON
exec weed server -dir=/data -s3 -s3.port=8333 -s3.config=/tmp/s3.json
