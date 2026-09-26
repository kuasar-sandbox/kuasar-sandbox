#!/usr/bin/env bash
# Build the OpenClaw long-term stability image and store its manifest.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROMPTS_FILE="$SCRIPT_DIR/prompts.jsonl"

if [[ ! -f "$PROMPTS_FILE" ]]; then
    echo "ERROR: prompts.jsonl not found: $PROMPTS_FILE" >&2
    echo "Please run the dataset download script first. python3 download_prompts.py" >&2
    exit 1
fi

echo "Using prompts file: $PROMPTS_FILE"

# Load user-specific configuration.
CONFIG_FILE="${CONFIG_FILE:-$SCRIPT_DIR/config.env}"

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "ERROR: config file not found: $CONFIG_FILE" >&2
    echo "fill in your own values in $CONFIG_FILE." >&2
    exit 1
fi

#sandbox and chat
STABILITY_TIMEOUT=86400
SANDBOX_COUNT=5
CHAT_INTERVAL=30
MONITOR_INTERVAL=30

# Memory leak heuristic:
# Alert if RSS grows by at least 512 MiB above baseline
# for 10 consecutive monitoring samples.
MEM_GROWTH_MB=512
MEM_GROWTH_SAMPLES=10


STABILITY_WORKDIR="${STABILITY_WORKDIR:-$HOME/openclaw_long_term_stability}"
mkdir -p "$STABILITY_WORKDIR"
STABILITY_WORKDIR="$(cd -- "$STABILITY_WORKDIR" && pwd)"
STABILITY_DIR="$STABILITY_WORKDIR/docker"
STABILITY_IMAGE="openclaw-long-term-stability:latest"
STABILITY_IMG="$STABILITY_WORKDIR/images/openclaw_long_term_stability.img"


source "$CONNECTOR_CONFIG"
for var in FLOATING_IP_BASE MGMT_NETNS MGMT_ADDRS; do
    if [[ -z "${!var:-}" ]]; then
        echo "ERROR: $var is not set or empty in $CONNECTOR_CONFIG" >&2
        exit 1
    fi
done
echo "FLOATING_IP_BASE=$FLOATING_IP_BASE"
echo "MGMT_NETNS=$MGMT_NETNS"
echo "MGMT_ADDRS=$MGMT_ADDRS"

for cmd in docker sudo; do
    command -v "$cmd" >/dev/null || { printf 'Missing command: %s\n' "$cmd" >&2; exit 1; }
done

for path_cmd in "$FLATTEN_CTL" "$MANIFEST_CTL"; do
    [ -x "$path_cmd" ] || { printf 'Missing or non-executable: %s\n' "$path_cmd" >&2; exit 1; }
done

# These names are required by the Kuasar tools and must remain unchanged.
[[ -r "$MANIFEST_CONFIG" ]] || { printf 'Cannot read: %s\n' "$MANIFEST_CONFIG" >&2; exit 1; }

mkdir -p "$STABILITY_DIR" \
    "$STABILITY_WORKDIR/images" \
    "$STABILITY_WORKDIR/logs" \
    "$STABILITY_WORKDIR/results"
printf 'Work directory: %s\n' "$STABILITY_WORKDIR"

printf '[1/4] Writing Dockerfile\n'
cat > "$STABILITY_DIR/Dockerfile" <<DOCKERFILE
FROM ghcr.io/openclaw/openclaw:latest

USER root

RUN --network=none usermod -l user node && \
    groupmod -n user node && \
    usermod -d /home/user -m user && \
    mkdir -p /home/user/.openclaw/workspace && \
    chown -R user:user /home/user/.openclaw

RUN cat << 'JSON' > /home/user/.openclaw/openclaw.json
{
  "agents": {
    "defaults": {
      "workspace": "/home/user/.openclaw/workspace",
      "model": {
        "primary": "local-qwen/qwen3"
      },
      "models": {
        "local-qwen/qwen3": {
          "params": {
            "maxTokens": 8192,
            "extra_body": {
              "tool_choice": "none",
              "chat_template_kwargs": {
                "enable_thinking": false
              }
            }
          }
        }
      },
      "sandbox": {
        "mode": "off"
      }
    }
  },
  "models": {
    "mode": "merge",
    "providers": {
      "local-qwen": {
        "baseUrl": "http://${MGMT_ADDRS%/*}:18000/v1",
        "apiKey": "local-no-key",
        "api": "openai-completions",
        "timeoutSeconds": 300,
        "request": {
          "allowPrivateNetwork": true
        },
        "models": [
          {
            "id": "qwen3",
            "name": "Qwen3-30B-A3B",
            "reasoning": false,
            "input": ["text"],
            "contextWindow": 32768,
            "maxTokens": 8192
          }
        ]
      }
    }
  },
  "gateway": {
    "mode": "local",
    "auth": {
      "mode": "token",
      "token": "testtoken123"
    },
    "port": 18789,
    "bind": "lan",
    "tailscale": {
      "mode": "off",
      "resetOnExit": false
    },
    "nodes": {
      "denyCommands": [
        "camera.snap",
        "camera.clip",
        "screen.record",
        "contacts.add",
        "calendar.add",
        "reminders.add",
        "sms.send",
        "sms.search"
      ]
    },
    "http": {
      "endpoints": {
        "chatCompletions": {
          "enabled": true
        }
      }
    }
  },
  "session": {
    "dmScope": "per-channel-peer"
  },
  "tools": {
    "profile": "coding"
  }
}
JSON
RUN chown -R user:user /home/user/.openclaw

USER user
WORKDIR /home/user
CMD ["node", "/app/openclaw.mjs", "gateway"]
DOCKERFILE

printf '[2/4] Building %s\n' "$STABILITY_IMAGE"
DOCKER_BUILDKIT=1 docker build \
    -t "$STABILITY_IMAGE" \
    "$STABILITY_DIR"

printf '[3/4] Exporting %s\n' "$STABILITY_IMG"
# flatten-ctl must run as root to preserve image file ownership (chown).
sudo -v
docker save "$STABILITY_IMAGE" | \
    sudo "$FLATTEN_CTL" export --output "$STABILITY_IMG" --no-progress

printf '[4/4] Storing manifest\n'
STABILITY_TEMPLATE_KEY="$("$MANIFEST_CTL" store "$STABILITY_IMG")"
[[ -n "$STABILITY_TEMPLATE_KEY" ]] || {
    printf 'manifest-ctl returned an empty template key\n' >&2
    exit 1
}

printf '\nSTABILITY_TEMPLATE_KEY: %s\n' "$STABILITY_TEMPLATE_KEY"

STABILITY_TID="e2b-img-$(echo -n "manifest://${STABILITY_TEMPLATE_KEY}" | base64 -w0 | tr '+/' '-_' | tr -d '=')"


SANDBOX_MAP="$STABILITY_WORKDIR/results/sandboxes.csv"
ALERT_LOG="$STABILITY_WORKDIR/logs/alerts.log"
CHAT_LOG="$STABILITY_WORKDIR/logs/chat.log"
MEMORY_LOG="$STABILITY_WORKDIR/logs/memory.csv"

mkdir -p \
    "$STABILITY_WORKDIR/logs" \
    "$STABILITY_WORKDIR/results"

rm -f "$STABILITY_WORKDIR/ALERT"

printf 'sandbox_id,slot_id,floating_ip\n' > "$SANDBOX_MAP"
printf 'timestamp,sandbox_id,pid,rss_kb,rss_mb,baseline_mb\n' > "$MEMORY_LOG"


alert() {
    local msg="$1"
    local ts

    ts="$(date '+%Y-%m-%d %H:%M:%S')"

    printf '\n[ALERT] %s %s\n' "$ts" "$msg" >&2
    printf '[ALERT] %s %s\n' "$ts" "$msg" >> "$ALERT_LOG"

    logger -t openclaw-stability -- "$msg" || true

    {
        printf '%s\n' "$ts"
        printf '%s\n' "$msg"
    } >> "$STABILITY_WORKDIR/ALERT"
}


###############################################################################
# 1. Check initial state
###############################################################################
cat <<'EOF' > "$STABILITY_WORKDIR/check_empty.py"
import sys
from e2b import Sandbox

paginator = Sandbox.list()
items = paginator.next_items()

running = [
    sb for sb in items
    if sb.state == "running"
]

if running:
    print("ERROR: Running E2B sandboxes found:")
    for sb in running:
        print(f"  - {sb.sandbox_id}")
    sys.exit(1)

print("No running E2B sandboxes found.")
EOF

python3 "$STABILITY_WORKDIR/check_empty.py"


ALLOCATED_COUNT=$(
    sudo "$CONNECTOR_CTL" vswitch show slots "$SWITCH_NAME" |
        jq '[.[] | select(.allocated == true)] | length'
)

if [[ "$ALLOCATED_COUNT" -ne 0 ]]; then
    echo "ERROR: connector switch already has $ALLOCATED_COUNT allocated port(s)." >&2
    exit 1
fi

printf 'Connector switch is empty.\n'


###############################################################################
# 2. Start LLM socat
###############################################################################

MGMT_IP="${MGMT_ADDRS%%/*}"

rm -f "$STABILITY_WORKDIR/llm.sock"

printf 'Starting host -> LLM socat...\n'

socat \
    UNIX-LISTEN:"$STABILITY_WORKDIR/llm.sock",fork,mode=0600 \
    TCP4:"$LLM_URL" \
    > "$STABILITY_WORKDIR/logs/socat_unix.log" 2>&1 &

SOCAT_UNIX_PID=$!


printf 'Starting management-netns socat...\n'

sudo ip netns exec "$MGMT_NETNS" \
    socat \
    TCP4-LISTEN:18000,bind="$MGMT_IP",reuseaddr,fork \
    UNIX-CONNECT:"$STABILITY_WORKDIR/llm.sock" \
    > "$STABILITY_WORKDIR/logs/socat_netns.log" 2>&1 &

SOCAT_NETNS_PID=$!

sleep 1


###############################################################################
# 3. Python helper: create ONE sandbox and start OpenClaw
###############################################################################

cat <<'EOF' > "$STABILITY_WORKDIR/create_openclaw.py"
import os
import sys
import time
from e2b import Sandbox

template_id = os.environ["STABILITY_TID"]
timeout = int(os.environ["STABILITY_TIMEOUT"])
sid_file = os.environ["SID_FILE"]

print(f"Creating sandbox from template: {template_id}")

sandbox = Sandbox.create(
    template_id,
    timeout=timeout,
)

sid = sandbox.sandbox_id

print(f"Sandbox ID: {sid}")

with open(sid_file, "w") as f:
    f.write(sid)

sandbox.commands.run(
    """
    nohup node /app/openclaw.mjs gateway \
      > /tmp/openclaw-gateway.log 2>&1 \
      < /dev/null &
    echo $! > /tmp/openclaw-gateway.pid
    """,
    envs={
        "OPENCLAW_CONFIG_PATH": "/home/user/.openclaw/openclaw.json"
    },
)

time.sleep(3)

result = sandbox.commands.run(
    """
    echo "=== PID ==="
    cat /tmp/openclaw-gateway.pid || true

    echo "=== PROCESS ==="
    ps -fp $(cat /tmp/openclaw-gateway.pid 2>/dev/null) || true

    echo "=== LOG ==="
    tail -n 30 /tmp/openclaw-gateway.log || true
    """
)

print(result.stdout)
EOF


###############################################################################
# 4. Create 5 sandboxes serially
#
# We intentionally create them one by one so we can identify exactly which new
# connector slot belongs to which E2B sandbox.
###############################################################################

declare -a SANDBOX_IDS
declare -a FLOATING_IPS


for i in $(seq 0 $((SANDBOX_COUNT - 1))); do

    printf '\n============================================================\n'
    printf 'Creating sandbox %d/%d\n' "$((i + 1))" "$SANDBOX_COUNT"
    printf '============================================================\n'

    BEFORE_FILE="$STABILITY_WORKDIR/results/slots-before-${i}.json"
    AFTER_FILE="$STABILITY_WORKDIR/results/slots-after-${i}.json"
    SID_FILE="$STABILITY_WORKDIR/results/sid-${i}"

    sudo "$CONNECTOR_CTL" \
        vswitch show slots "$SWITCH_NAME" \
        > "$BEFORE_FILE"

    STABILITY_TID="$STABILITY_TID" \
    STABILITY_TIMEOUT="$STABILITY_TIMEOUT" \
    SID_FILE="$SID_FILE" \
        python3 "$STABILITY_WORKDIR/create_openclaw.py"

    SID="$(cat "$SID_FILE")"

    [[ -n "$SID" ]] || {
        alert "Failed to obtain sandbox ID for sandbox index $i"
        exit 1
    }

    #
    # Wait for connector auto-attach.
    #
    NEW_SLOT=""

    for _ in $(seq 1 60); do

        sudo "$CONNECTOR_CTL" \
            vswitch show slots "$SWITCH_NAME" \
            > "$AFTER_FILE"

        NEW_SLOT=$(
            jq -n \
                --slurpfile before "$BEFORE_FILE" \
                --slurpfile after "$AFTER_FILE" '
                    ($before[0]
                        | map(select(.allocated == true))
                        | map(.slot_id)) as $old
                    |
                    $after[0][]
                    | select(.allocated == true)
                    | select((.slot_id as $id | $old | index($id)) == null)
                    | .slot_id
                ' |
                head -n1
        )

        if [[ -n "$NEW_SLOT" ]]; then
            break
        fi

        sleep 1
    done

    if [[ -z "$NEW_SLOT" ]]; then
        alert "Sandbox $SID was created, but no new connector slot appeared"
        exit 1
    fi

    FLOATING_IP=$(
        jq -r \
            --argjson slot "$NEW_SLOT" \
            '.[] | select(.slot_id == $slot) | .floating_ip' \
            "$AFTER_FILE"
    )

    if [[ -z "$FLOATING_IP" || "$FLOATING_IP" == "null" ]]; then
        alert "Could not determine floating IP for SID=$SID slot=$NEW_SLOT"
        exit 1
    fi

    SANDBOX_IDS+=("$SID")
    FLOATING_IPS+=("$FLOATING_IP")

    printf '%s,%s,%s\n' \
        "$SID" \
        "$NEW_SLOT" \
        "$FLOATING_IP" \
        >> "$SANDBOX_MAP"

    printf 'Mapped:\n'
    printf '  SID:         %s\n' "$SID"
    printf '  slot:        %s\n' "$NEW_SLOT"
    printf '  floating IP: %s\n' "$FLOATING_IP"


    #
    # Wait until gateway is reachable through floating IP.
    #
    READY=0

    for _ in $(seq 1 60); do

        if sudo ip netns exec "$MGMT_NETNS" \
            curl --noproxy '*' \
            -fsS \
            --connect-timeout 2 \
            --max-time 3 \
            "http://${FLOATING_IP}:18789/healthz" \
            >/dev/null 2>&1
        then
            READY=1
            break
        fi

        sleep 1
    done

    if [[ "$READY" -ne 1 ]]; then
        alert "OpenClaw gateway failed health check: SID=$SID IP=$FLOATING_IP"
        exit 1
    fi

    printf 'Gateway ready: %s\n' "$FLOATING_IP"
done


###############################################################################
# 5. Print mapping
###############################################################################

printf '\n============================================================\n'
printf 'Sandbox mapping\n'
printf '============================================================\n'

column -s, -t "$SANDBOX_MAP" || cat "$SANDBOX_MAP"


###############################################################################
# 6. Background memory / process monitor
###############################################################################

monitor_memory() {

    declare -A BASE_RSS
    declare -A HIGH_COUNT

    while true; do

        for sid in "${SANDBOX_IDS[@]}"; do

            #
            # SID appears in the cloud-hypervisor API socket path.
            #
            pid=$(
                pgrep -f \
                    "cloud-hypervisor .*sandbox-tyler/${sid}/ch.sock" |
                    head -n1 || true
            )

            if [[ -z "$pid" ]]; then
                alert "cloud-hypervisor disappeared: SID=$sid"
                continue
            fi

            rss_kb=$(
                ps -o rss= -p "$pid" 2>/dev/null |
                    awk '{print $1}'
            )

            if [[ -z "$rss_kb" ]]; then
                alert "Unable to read RSS: SID=$sid PID=$pid"
                continue
            fi

            rss_mb=$((rss_kb / 1024))

            if [[ -z "${BASE_RSS[$sid]+x}" ]]; then
                BASE_RSS["$sid"]="$rss_kb"
                HIGH_COUNT["$sid"]=0
            fi

            baseline_kb="${BASE_RSS[$sid]}"
            baseline_mb=$((baseline_kb / 1024))

            printf '%s,%s,%s,%s,%s,%s\n' \
                "$(date '+%Y-%m-%d %H:%M:%S')" \
                "$sid" \
                "$pid" \
                "$rss_kb" \
                "$rss_mb" \
                "$baseline_mb" \
                >> "$MEMORY_LOG"

            growth_kb=$((rss_kb - baseline_kb))
            threshold_kb=$((MEM_GROWTH_MB * 1024))

            if (( growth_kb >= threshold_kb )); then

                HIGH_COUNT["$sid"]=$(( ${HIGH_COUNT[$sid]} + 1 ))

                if (( ${HIGH_COUNT[$sid]} >= MEM_GROWTH_SAMPLES )); then

                    growth_mb=$((growth_kb / 1024))

                    alert \
                        "Possible memory leak: SID=$sid PID=$pid RSS=${rss_mb}MB baseline=${baseline_mb}MB growth=${growth_mb}MB"

                    #
                    # Don't spam every 30 seconds once the condition fires.
                    #
                    HIGH_COUNT["$sid"]=0
                fi

            else
                HIGH_COUNT["$sid"]=0
            fi
        done

        sleep "$MONITOR_INTERVAL"
    done
}


monitor_memory &
MONITOR_PID=$!

printf 'Memory monitor PID: %s\n' "$MONITOR_PID"


###############################################################################
# 7. Chat workload
###############################################################################
PROMPT_FILE="$(dirname "$0")/prompts.jsonl"
CHAT_RESPONSE_LOG="$STABILITY_WORKDIR/logs/chat_responses.jsonl"

if [[ ! -s "$PROMPT_FILE" ]]; then
    echo "ERROR: prompt file missing or empty: $PROMPT_FILE" >&2
    exit 1
fi


declare -A CHAT_COUNT

send_chat() {
    local sid="$1"
    local ip="$2"
    local index="$3"

    local ts
    local prompt
    local response
    local user

    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    user="stability-${index}"

    CHAT_COUNT[$index]=$(( ${CHAT_COUNT[$index]:-0} + 1 ))

    # Reset the OpenClaw session every 20 requests.
    if (( CHAT_COUNT[$index] % 20 == 0 )); then
        sudo ip netns exec "$MGMT_NETNS" \
            curl --noproxy '*' \
            -fsS \
            --connect-timeout 5 \
            --max-time 30 \
            "http://${ip}:18789/v1/chat/completions" \
            -H 'Authorization: Bearer testtoken123' \
            -H 'Content-Type: application/json' \
            -d "$(jq -n \
                --arg user "$user" \
                '{
                    model: "openclaw:main",
                    user: $user,
                    messages: [
                        {
                            role: "user",
                            content: "/reset"
                        }
                    ]
                }')" \
            >/dev/null || {
                alert "Failed to reset chat session: SID=$sid IP=$ip"
                return 1
            }

        printf '[%s] RESET SID=%s IP=%s\n' \
            "$ts" "$sid" "$ip" \
            >> "$CHAT_LOG"
    fi

    prompt="$(
        shuf -n 1 "$PROMPT_FILE" |
        jq -r '.prompt'
    )"

    if [[ -z "$prompt" || "$prompt" == "null" ]]; then
        alert "Failed to select prompt from $PROMPT_FILE"
        return 1
    fi

    if ! response=$(
        sudo ip netns exec "$MGMT_NETNS" \
            curl --noproxy '*' \
            -fsS \
            --connect-timeout 5 \
            --max-time 300 \
            "http://${ip}:18789/v1/chat/completions" \
            -H 'Authorization: Bearer testtoken123' \
            -H 'Content-Type: application/json' \
            -d "$(jq -n \
                --arg user "$user" \
                --arg prompt "$prompt" \
                '{
                    model: "openclaw:main",
                    user: $user,
                    messages: [
                        {
                            role: "user",
                            content: $prompt
                        }
                    ]
                }')"
    ); then
        alert "Chat request failed: SID=$sid IP=$ip"
        return 1
    fi

    jq -cn \
        --arg timestamp "$ts" \
        --arg sid "$sid" \
        --arg ip "$ip" \
        --arg prompt "$prompt" \
        --argjson response "$response" \
        '{
            timestamp: $timestamp,
            sandbox_id: $sid,
            floating_ip: $ip,
            prompt: $prompt,
            response: $response
        }' \
        >> "$CHAT_RESPONSE_LOG"

    printf '[%s] OK SID=%s IP=%s\n' \
        "$ts" \
        "$sid" \
        "$ip" \
        >> "$CHAT_LOG"
}

workload_loop() {

    local round=0

    while true; do

        round=$((round + 1))

        start_time="$(date +%s)"

        printf '[%s] Starting chat round %d\n' \
            "$(date '+%Y-%m-%d %H:%M:%S')" \
            "$round" \
            >> "$CHAT_LOG"

        declare -a CHAT_PIDS=()

        for i in "${!SANDBOX_IDS[@]}"; do

            send_chat \
                "${SANDBOX_IDS[$i]}" \
                "${FLOATING_IPS[$i]}" \
                "$i" &

            CHAT_PIDS+=("$!")
        done

        #
        # Wait for all 5 requests in this round.
        #
        for pid in "${CHAT_PIDS[@]}"; do
            wait "$pid" || true
        done

        end_time="$(date +%s)"
        elapsed=$((end_time - start_time))

        #
        # Start each round approximately every 30 seconds.
        #
        if (( elapsed < CHAT_INTERVAL )); then
            sleep $((CHAT_INTERVAL - elapsed))
        fi
    done
}


workload_loop &
WORKLOAD_PID=$!

printf 'Chat workload PID: %s\n' "$WORKLOAD_PID"


###############################################################################
# 8. Cleanup
###############################################################################

cleanup() {

    printf '\nStopping stability test...\n'

    kill "$WORKLOAD_PID" 2>/dev/null || true
    kill "$MONITOR_PID" 2>/dev/null || true

    kill "$SOCAT_UNIX_PID" 2>/dev/null || true

    sudo kill "$SOCAT_NETNS_PID" 2>/dev/null || true
}

trap cleanup EXIT INT TERM


###############################################################################
# Keep script alive
###############################################################################

printf '\n============================================================\n'
printf 'OpenClaw long-term stability test started\n'
printf '============================================================\n'
printf 'Sandboxes:        %s\n' "$SANDBOX_COUNT"
printf 'Chat interval:    %ss\n' "$CHAT_INTERVAL"
printf 'Monitor interval: %ss\n' "$MONITOR_INTERVAL"
printf 'Memory threshold: +%s MB for %s consecutive samples\n' \
    "$MEM_GROWTH_MB" \
    "$MEM_GROWTH_SAMPLES"

printf '\nMapping:\n'
cat "$SANDBOX_MAP"

printf '\nLogs:\n'
printf '  Chat:    %s\n' "$CHAT_LOG"
printf '  Memory:  %s\n' "$MEMORY_LOG"
printf '  Alerts:  %s\n' "$ALERT_LOG"

printf '\nPress Ctrl-C to stop.\n'

wait "$WORKLOAD_PID"
