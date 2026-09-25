#!/usr/bin/env bash
# =============================================================================
#  FortiCNAPP - Azure Automated Integration Preflight          (zero-touch)
# -----------------------------------------------------------------------------
#  Run it as-is. No arguments, no prompts. Works in Azure Cloud Shell (Bash)
#  or any shell where `az login` has been done.
#
#  Everything is discovered from the signed-in console session:
#
#    Tenant          current `az` tenant
#    Subscriptions   every subscription the console user can see in that tenant
#    Principals      (a) the console user        -> the identity that runs the
#                                                    automated integration
#                    (b) any existing FortiCNAPP / Lacework Service Principal
#                        found in the tenant (by display name)
#
#  For each principal it validates:
#    - Microsoft Entra ID: Application Administrator + Privileged Role
#      Administrator, active and tenant-wide (Global Administrator satisfies
#      both; direct, group-based, PIM-eligible-only and AU-scoped are handled)
#    - Azure RBAC: Owner on every subscription (direct, inherited from a
#      management group / root, or through group membership)
#    - Service Principals only: enabled, credential expiry
#
#  For each subscription it validates:
#    - State = Enabled
#    - Resource providers used by Activity Log + agentless scanning
#    - Free Activity Log diagnostic-setting slot (Azure max = 5) and any
#      existing FortiCNAPP/Lacework setting
#
#  READ-ONLY: nothing is changed. Remediation commands are printed only.
#  A JSON report is written to the current directory.
#
#  Exit codes:  0 = PASS (warnings allowed)
#               1 = FAIL (one or more blocking checks failed)
#               2 = UNABLE TO COMPLETE (checks could not be evaluated)
#
#  Optional environment overrides (never required):
#    FORTICNAPP_APP_ID=<appId>            check this SP instead of discovering
#    FORTICNAPP_SUBSCRIPTIONS=<id,id>     limit to these subscriptions
#    FORTICNAPP_SKIP_AGENTLESS=1          skip agentless provider checks
#    FORTICNAPP_EXPIRY_DAYS=30            credential-expiry warning window
#    NO_COLOR=1                           plain output
# =============================================================================

set -uo pipefail

readonly VERSION="3.1.0"
readonly GRAPH="https://graph.microsoft.com/v1.0"

# Entra ID built-in role template IDs (identical in every tenant)
readonly ROLE_GLOBAL_ADMIN="62e90394-69f5-4237-9190-012177145e10"
readonly ROLE_APP_ADMIN="9b895d92-2cd3-44c7-9d02-a6ac2d5ea5c3"
readonly ROLE_PRIV_ROLE_ADMIN="e8611ab8-c189-46e8-94e1-60213ab1f814"

# Display-name keywords used to discover an existing FortiCNAPP SP
readonly SP_KEYWORDS=(lacework forticnapp)

# Resource providers used by the integration components
readonly PROVIDERS_ACTIVITY_LOG=(Microsoft.Insights Microsoft.Storage Microsoft.EventGrid)
readonly PROVIDERS_AGENTLESS=(Microsoft.Compute Microsoft.Network Microsoft.App Microsoft.KeyVault Microsoft.ManagedIdentity)

readonly DIAG_SETTINGS_MAX=5

EXPIRY_WARN_DAYS="${FORTICNAPP_EXPIRY_DAYS:-30}"
CHECK_AGENTLESS=1; [[ "${FORTICNAPP_SKIP_AGENTLESS:-0}" == "1" ]] && CHECK_AGENTLESS=0

# -----------------------------------------------------------------------------
# State
# -----------------------------------------------------------------------------
N_PASS=0; N_WARN=0; N_FAIL=0; N_ERROR=0
CHECKS_JSON=(); MISSING=(); REMEDIATION=()

TENANT_ID=""; CALLER=""; CALLER_TYPE=""
SUBS_JSON='[]'                                # [{id,name,state}]
P_IDS=(); P_TYPES=(); P_NAMES=(); P_APPIDS=(); P_LABELS=()

# -----------------------------------------------------------------------------
# Output helpers
# -----------------------------------------------------------------------------
if [[ -z "${NO_COLOR:-}" && -t 1 ]]; then
    RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; BLU=$'\e[36m'; BLD=$'\e[1m'; RST=$'\e[0m'
else
    RED=""; GRN=""; YEL=""; BLU=""; BLD=""; RST=""
fi

banner()  { printf '%s\n' "${BLD}======================================================================${RST}"; }
section() { printf '\n%s[%s]%s\n' "$BLD" "$1" "$RST"; }

# record STATUS AREA CHECK [DETAIL]
record() {
    local status="$1" area="$2" check="$3" detail="${4:-}" color=""
    case "$status" in
        PASS)  N_PASS=$((N_PASS + 1));   color="$GRN" ;;
        WARN)  N_WARN=$((N_WARN + 1));   color="$YEL" ;;
        FAIL)  N_FAIL=$((N_FAIL + 1));   color="$RED" ;;
        ERROR) N_ERROR=$((N_ERROR + 1)); color="$RED" ;;
        INFO)  color="$BLU" ;;
    esac
    printf '  %s%-5s%s  %s\n' "$color" "$status" "$RST" "$check"
    [[ -n "$detail" ]] && printf '         %s\n' "$detail"
    CHECKS_JSON+=("$(jq -nc --arg s "$status" --arg a "$area" --arg c "$check" --arg d "$detail" \
        '{status:$s, area:$a, check:$c, detail:$d}')")
}
missing() { MISSING+=("$1"); }
remedy()  { REMEDIATION+=("$1"); }

# GET a Graph collection, following @odata.nextLink. Prints a JSON array.
graph_get_all() {
    local url="$1" page all='[]'
    shift
    while [[ -n "$url" ]]; do
        page=$(az rest --method GET --url "$url" "$@" -o json 2>/dev/null) || return 1
        all=$(jq -nc --argjson a "$all" --argjson p "$page" '$a + ($p.value // [])') || return 1
        url=$(jq -r '."@odata.nextLink" // empty' <<<"$page")
    done
    printf '%s' "$all"
}

usage() { sed -n '2,50p' "$0" | sed 's/^#  \{0,1\}//'; }

# Portable "run this, but kill it after N seconds" — macOS has no `timeout` binary by default
# (only `gtimeout` from brew's coreutils), so fall back to a background job + `wait` with our own
# watchdog when neither is present, instead of silently running unbounded.
run_with_timeout() {  # seconds cmd...
    local secs="$1"; shift
    if command -v timeout >/dev/null 2>&1; then
        timeout "$secs" "$@"
    elif command -v gtimeout >/dev/null 2>&1; then
        gtimeout "$secs" "$@"
    else
        "$@" &
        local pid=$! waited=0
        while kill -0 "$pid" 2>/dev/null; do
            sleep 1; waited=$((waited + 1))
            if [[ "$waited" -ge "$secs" ]]; then
                kill -9 "$pid" 2>/dev/null
                wait "$pid" 2>/dev/null
                return 124
            fi
        done
        wait "$pid"
    fi
}

# -----------------------------------------------------------------------------
# 1. Tooling & session
# -----------------------------------------------------------------------------
check_session() {
    section "1. Tooling & console session"
    local tool
    for tool in az jq; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            record FAIL tooling "$tool installed" "Install $tool (or run from Azure Cloud Shell) and re-run."
            finish
        fi
    done
    record PASS tooling "az CLI and jq installed" \
        "azure-cli $(az version -o json 2>/dev/null | jq -r '."azure-cli" // "unknown"')"

    local account
    if ! account=$(az account show -o json 2>/dev/null); then
        record FAIL tooling "Azure CLI signed in" "No active session. Run: az login"
        finish
    fi
    TENANT_ID=$(jq -r '.tenantId' <<<"$account")
    CALLER=$(jq -r '.user.name' <<<"$account")
    CALLER_TYPE=$(jq -r '.user.type' <<<"$account")
    record PASS tooling "Azure CLI signed in" "Console identity: $CALLER ($CALLER_TYPE) | Tenant: $TENANT_ID"

    if az account get-access-token --resource-type ms-graph -o none 2>/dev/null; then
        record PASS tooling "Microsoft Graph token available"
    else
        record ERROR tooling "Microsoft Graph token available" "Entra ID checks cannot run. Re-run: az login"
        finish
    fi
}

# -----------------------------------------------------------------------------
# 2. Discovery
# -----------------------------------------------------------------------------
discover_subscriptions() {
    section "2. Discovery"
    local all others
    if ! all=$(az account list --all -o json 2>/dev/null); then
        record ERROR discovery "List subscriptions" "az account list failed."
        finish
    fi

    SUBS_JSON=$(jq -c --arg t "$TENANT_ID" \
        '[ .[] | select(.tenantId==$t) | {id, name, state} ] | unique_by(.id) | sort_by(.name)' <<<"$all")

    if [[ -n "${FORTICNAPP_SUBSCRIPTIONS:-}" ]]; then
        SUBS_JSON=$(jq -c --arg ids "${FORTICNAPP_SUBSCRIPTIONS// /}" \
            '($ids | ascii_downcase | split(",")) as $w | [ .[] | select((.id|ascii_downcase) as $i | $w | index($i)) ]' <<<"$SUBS_JSON")
    fi

    local count; count=$(jq 'length' <<<"$SUBS_JSON")
    if [[ "$count" -eq 0 ]]; then
        record FAIL discovery "Subscriptions visible to console user" "None found in tenant $TENANT_ID."
        missing "Console user has no visible subscription in tenant $TENANT_ID"
        finish
    fi
    record PASS discovery "Subscriptions visible to console user" "$count subscription(s) in tenant $TENANT_ID"
    jq -r '.[] | "           - \(.name)  (\(.id))  [\(.state)]"' <<<"$SUBS_JSON"

    others=$(jq -r --arg t "$TENANT_ID" '[ .[] | select(.tenantId!=$t) | .tenantId ] | unique | length' <<<"$all")
    [[ "$others" -gt 0 ]] && record INFO discovery "Other tenants" \
        "Subscriptions in $others other tenant(s) are not checked. Run 'az login --tenant <id>' to check them."
}

add_principal() {  # id type name appId label
    P_IDS+=("$1"); P_TYPES+=("$2"); P_NAMES+=("$3"); P_APPIDS+=("$4"); P_LABELS+=("$5")
}

discover_principals() {
    # (a) Console identity
    local me
    if [[ "$CALLER_TYPE" == "user" ]]; then
        if me=$(az ad signed-in-user show -o json 2>/dev/null); then
            add_principal "$(jq -r '.id' <<<"$me")" User \
                "$(jq -r '.userPrincipalName // .displayName' <<<"$me")" "" "Console user (deployer)"
        else
            record ERROR discovery "Resolve console user" "az ad signed-in-user show failed."
        fi
    else
        if me=$(az ad sp show --id "$CALLER" -o json 2>/dev/null); then
            add_principal "$(jq -r '.id' <<<"$me")" ServicePrincipal \
                "$(jq -r '.displayName' <<<"$me")" "$(jq -r '.appId' <<<"$me")" "Console identity (deployer SP)"
        else
            record ERROR discovery "Resolve console identity" "Could not resolve SP $CALLER."
        fi
    fi

    # (b) FortiCNAPP / Lacework Service Principal(s)
    local sps='[]' kw found
    if [[ -n "${FORTICNAPP_APP_ID:-}" ]]; then
        if found=$(az ad sp show --id "$FORTICNAPP_APP_ID" -o json 2>/dev/null); then
            sps=$(jq -c '[.]' <<<"$found")
        else
            record FAIL discovery "FortiCNAPP Service Principal" "FORTICNAPP_APP_ID=$FORTICNAPP_APP_ID not found."
            missing "Service Principal $FORTICNAPP_APP_ID"
        fi
    elif [[ -n "${FORTICNAPP_SUBSCRIPTIONS:-}" ]]; then
        # Scoped run: never touch the tenant. Instead of a tenant-wide Graph search, list the role
        # assignments that already exist on each scoped subscription, keep only the ServicePrincipal
        # ones, and resolve just those - a handful of calls bounded by what's actually on this
        # subscription, not by tenant size.
        local subn j sub_id raw sp_ids sp_id found_kw kw_match
        subn=$(jq 'length' <<<"$SUBS_JSON")
        sp_ids='[]'
        echo "  ...listing role assignments on $subn scoped subscription(s) to find Service Principals in scope" >&2
        for ((j = 0; j < subn; j++)); do
            sub_id=$(jq -r ".[$j].id" <<<"$SUBS_JSON")
            if raw=$(run_with_timeout "${AZ_CALL_TIMEOUT:-20}" az role assignment list \
                    --scope "/subscriptions/$sub_id" --include-inherited \
                    --fill-principal-name false -o json 2>/dev/null); then
                sp_ids=$(jq -nc --argjson a "$sp_ids" --argjson b "$raw" \
                    '$a + [ $b[] | select(.principalType=="ServicePrincipal") | .principalId ] | unique')
            else
                echo "      - role assignment list on $sub_id timed out/failed, skipping" >&2
            fi
        done

        local n; n=$(jq 'length' <<<"$sp_ids")
        if [[ "$n" -gt 0 ]]; then
            echo "  ...resolving $n Service Principal(s) found on scoped subscription(s) via Graph (batched)" >&2
            local sp_id_arr=() chunk_size=15 start end chunk_ids id ids_quoted joined filter_raw url resolved kw_ok
            mapfile -t sp_id_arr < <(jq -r '.[]' <<<"$sp_ids")
            for ((start = 0; start < n; start += chunk_size)); do
                end=$((start + chunk_size)); [[ "$end" -gt "$n" ]] && end="$n"
                chunk_ids=("${sp_id_arr[@]:$start:$((end - start))}")
                ids_quoted=()
                for id in "${chunk_ids[@]}"; do ids_quoted+=("'$id'"); done
                joined=$(IFS=,; echo "${ids_quoted[*]}")
                filter_raw="id in ($joined)"
                url="$GRAPH/servicePrincipals?\$filter=$(jq -rn --arg f "$filter_raw" '$f|@uri')&\$select=id,appId,displayName&\$count=true"
                # One Graph call resolves up to 15 SPs at once, instead of one az ad sp show per SP -
                # both faster and each call is individually bounded by run_with_timeout.
                if resolved=$(run_with_timeout "${AZ_CALL_TIMEOUT:-20}" az rest --method GET --url "$url" \
                        --headers "ConsistencyLevel=eventual" -o json 2>/dev/null); then
                    resolved=$(jq -c '.value // []' <<<"$resolved")
                else
                    echo "      - batched Graph lookup failed for ${#chunk_ids[@]} SP(s), resolving individually (bounded)" >&2
                    resolved='[]'
                    for id in "${chunk_ids[@]}"; do
                        found=$(run_with_timeout 10 az ad sp show --id "$id" -o json 2>/dev/null) || continue
                        resolved=$(jq -nc --argjson a "$resolved" --argjson b "[$found]" '$a + $b')
                    done
                fi
                kw_ok=$(jq -c --arg kw_list "${SP_KEYWORDS[*]}" \
                    '($kw_list | ascii_downcase | split(" ")) as $kws
                     | [ .[] | select(((.displayName // "") | ascii_downcase) as $d | $kws | any(. as $k | $d | contains($k))) ]' \
                    <<<"$resolved")
                sps=$(jq -nc --argjson a "$sps" --argjson b "$kw_ok" '$a + $b | unique_by(.id)')
            done
        fi
        record INFO discovery "Service Principal scoping" \
            "Scanned Service Principals with a role on the scoped subscription(s) only ($n candidate(s) checked), not the whole tenant."
    else
        for kw in "${SP_KEYWORDS[@]}"; do
            # $search matches any word in displayName; fall back to startswith if unsupported
            found=$(graph_get_all \
                "$GRAPH/servicePrincipals?%24search=%22displayName:${kw}%22&%24select=id,appId,displayName&%24top=999" \
                --headers "ConsistencyLevel=eventual") || \
            found=$(az ad sp list --filter "startswith(displayName,'${kw}')" \
                --query "[].{id:id,appId:appId,displayName:displayName}" -o json 2>/dev/null) || found='[]'
            # Graph's $search does fuzzy/tokenized matching, not strict substring containment - on a
            # large tenant it can return far more SPs than actually match (seen: 277 "hits" for one
            # keyword). Re-check displayName actually contains the keyword before keeping anything.
            local raw_n kept_n
            raw_n=$(jq 'length' <<<"$found")
            found=$(jq -c --arg kw "$kw" \
                '[ .[] | select((.displayName // "") | ascii_downcase | contains($kw | ascii_downcase)) ]' \
                <<<"$found")
            kept_n=$(jq 'length' <<<"$found")
            [[ "$raw_n" -ne "$kept_n" ]] && echo "  ...keyword '$kw': Graph returned $raw_n fuzzy match(es), $kept_n actually contain it - discarding the rest" >&2
            sps=$(jq -nc --argjson a "$sps" --argjson b "$found" '$a + $b | unique_by(.id)')
        done
    fi

    local n; n=$(jq 'length' <<<"$sps")
    if [[ "$n" -eq 0 ]]; then
        record INFO discovery "Existing FortiCNAPP Service Principal" \
            "None found (keywords: ${SP_KEYWORDS[*]}). The automated integration will create it; only the console user is evaluated."
    else
        record INFO discovery "Existing FortiCNAPP Service Principal" "$n found - each will be evaluated."
        local i
        for ((i = 0; i < n; i++)); do
            add_principal "$(jq -r ".[$i].id" <<<"$sps")" ServicePrincipal \
                "$(jq -r ".[$i].displayName" <<<"$sps")" "$(jq -r ".[$i].appId" <<<"$sps")" "FortiCNAPP SP"
            printf '           - %s  (appId %s)\n' "$(jq -r ".[$i].displayName" <<<"$sps")" "$(jq -r ".[$i].appId" <<<"$sps")"
        done
    fi

    [[ ${#P_IDS[@]} -eq 0 ]] && { record ERROR discovery "Principals to evaluate" "None resolved."; finish; }
}

# -----------------------------------------------------------------------------
# 3. Principal checks
# -----------------------------------------------------------------------------
check_sp_health() {  # objectId appId
    local sp pw cert fed creds now
    sp=$(az ad sp show --id "$1" -o json 2>/dev/null) || return
    if [[ "$(jq -r '.accountEnabled' <<<"$sp")" == "true" ]]; then
        record PASS principal "Service Principal enabled"
    else
        record FAIL principal "Service Principal enabled" "accountEnabled=false; sign-in is blocked."
        missing "Service Principal $2 is disabled"
    fi

    if ! pw=$(az ad app credential list --id "$2" -o json 2>/dev/null) || \
       ! cert=$(az ad app credential list --id "$2" --cert -o json 2>/dev/null); then
        record INFO principal "Credential expiry" "App registration not readable from this tenant/user - skipped."
        return
    fi
    fed=$(az ad app federated-credential list --id "$2" -o json 2>/dev/null || echo '[]')
    now=$(date -u +%s)
    creds=$(jq -nc --argjson p "$pw" --argjson k "$cert" --argjson now "$now" '
        def epoch: sub("\\.[0-9]+"; "") | sub("\\+00:00$"; "Z") | fromdateiso8601;
        [ $p[], $k[] ] | map(((.endDateTime | epoch) - $now) / 86400 | floor)')

    local total valid soonest fed_count
    total=$(jq 'length' <<<"$creds")
    valid=$(jq '[.[] | select(. >= 0)] | length' <<<"$creds")
    soonest=$(jq -r '[.[] | select(. >= 0)] | min // empty' <<<"$creds")
    fed_count=$(jq 'length' <<<"$fed")

    if   [[ "$total" -eq 0 && "$fed_count" -gt 0 ]]; then
        record PASS principal "Credentials" "$fed_count federated credential(s)."
    elif [[ "$total" -eq 0 ]]; then
        record WARN principal "Credentials" "No secrets, certificates or federated credentials."
    elif [[ "$valid" -eq 0 ]]; then
        record FAIL principal "Credentials" "All $total secret(s)/certificate(s) expired."
        missing "Valid credential on SP $2"
        remedy "az ad app credential reset --id $2 --append --years 1"
    elif [[ "$soonest" -lt "$EXPIRY_WARN_DAYS" ]]; then
        record WARN principal "Credentials" "$valid valid; earliest expires in $soonest day(s)."
    else
        record PASS principal "Credentials" "$valid valid; earliest expires in $soonest day(s)."
    fi
}

check_entra() {  # objectId type label
    local id="$1" type="$2" label="$3" coll active scoped eligible filter
    [[ "$type" == "User" ]] && coll="users" || coll="servicePrincipals"
    filter="%24filter=principalId%20eq%20'${id}'"

    # Active, tenant-wide roles incl. those granted through role-assignable groups
    if ! active=$(graph_get_all "$GRAPH/$coll/$id/transitiveMemberOf/microsoft.graph.directoryRole?%24select=roleTemplateId,displayName"); then
        # Fallback: direct assignments only
        if ! active=$(graph_get_all "$GRAPH/roleManagement/directory/roleAssignments?${filter}" \
                | jq -c '[ .[] | select(.directoryScopeId=="/") | {roleTemplateId:.roleDefinitionId} ]'); then
            record ERROR entra "Read Entra ID roles" "Graph query failed; directory read permission required."
            return
        fi
    fi
    scoped=$(graph_get_all "$GRAPH/roleManagement/directory/roleAssignments?${filter}" 2>/dev/null \
                | jq -c '[ .[] | select(.directoryScopeId!="/") ]' 2>/dev/null || echo '[]')
    eligible=$(graph_get_all "$GRAPH/roleManagement/directory/roleEligibilityScheduleInstances?${filter}" 2>/dev/null || echo '[]')

    record INFO entra "Active Entra ID roles" \
        "$(jq -r '[ .[] | (.displayName // .roleTemplateId // empty) ] | unique | join(", ") | if .=="" then "none" else . end' <<<"$active")"

    local role_id role_name
    for role_id in "$ROLE_APP_ADMIN" "$ROLE_PRIV_ROLE_ADMIN"; do
        [[ "$role_id" == "$ROLE_APP_ADMIN" ]] && role_name="Application Administrator" || role_name="Privileged Role Administrator"
        if jq -e --arg r "$role_id" 'any(.[]; .roleTemplateId==$r)' <<<"$active" >/dev/null; then
            record PASS entra "$role_name" "Active, tenant-wide."
        elif jq -e --arg r "$ROLE_GLOBAL_ADMIN" 'any(.[]; .roleTemplateId==$r)' <<<"$active" >/dev/null; then
            record PASS entra "$role_name" "Satisfied by Global Administrator."
        elif jq -e --arg r "$role_id" 'any(.[]; .roleDefinitionId==$r)' <<<"$eligible" >/dev/null; then
            record FAIL entra "$role_name" "PIM-eligible only - activate it before running the integration."
            missing "$label: activate PIM role $role_name"
        elif jq -e --arg r "$role_id" 'any(.[]; .roleDefinitionId==$r)' <<<"$scoped" >/dev/null; then
            record FAIL entra "$role_name" "Assigned only at administrative-unit/app scope; tenant-wide required."
            missing "$label: $role_name (tenant-wide)"
            remedy "az rest --method POST --url $GRAPH/roleManagement/directory/roleAssignments --body '{\"principalId\":\"$id\",\"roleDefinitionId\":\"$role_id\",\"directoryScopeId\":\"/\"}'   # $role_name -> $label"
        else
            record FAIL entra "$role_name" "Not assigned."
            missing "$label: Entra ID $role_name"
            remedy "az rest --method POST --url $GRAPH/roleManagement/directory/roleAssignments --body '{\"principalId\":\"$id\",\"roleDefinitionId\":\"$role_id\",\"directoryScopeId\":\"/\"}'   # $role_name -> $label"
        fi
    done
}

list_role_assignments() {  # objectId scope
    local out
    out=$(az role assignment list --assignee-object-id "$1" --scope "$2" \
            --include-inherited --include-groups --fill-principal-name false -o json 2>/dev/null) || \
    out=$(az role assignment list --assignee-object-id "$1" --scope "$2" \
            --include-inherited --fill-principal-name false -o json 2>/dev/null) || return 1
    printf '%s' "$out"
}

check_rbac() {  # objectId type label
    local id="$1" type="$2" label="$3" i n sub name assignments owner_scopes ok=0 bad=0
    n=$(jq 'length' <<<"$SUBS_JSON")
    for ((i = 0; i < n; i++)); do
        sub=$(jq -r ".[$i].id" <<<"$SUBS_JSON"); name=$(jq -r ".[$i].name" <<<"$SUBS_JSON")
        if ! assignments=$(list_role_assignments "$id" "/subscriptions/$sub"); then
            record ERROR rbac "Owner on $name" "Cannot read role assignments on /subscriptions/$sub."
            continue
        fi
        owner_scopes=$(jq -r '[ .[] | select((.roleDefinitionName // "" | ascii_downcase) == "owner") | .scope ]
                              | unique | join(", ")' <<<"$assignments")
        if [[ -n "$owner_scopes" ]]; then
            ok=$((ok + 1))
            record PASS rbac "Owner on $name" "Granted at: $owner_scopes"
        else
            bad=$((bad + 1))
            record FAIL rbac "Owner on $name" "Effective roles: $(jq -r '[.[].roleDefinitionName] | unique | join(", ") | if .=="" then "none" else . end' <<<"$assignments")"
            missing "$label: Azure Owner on $name ($sub)"
            remedy "az role assignment create --assignee-object-id $id --assignee-principal-type $type --role Owner --scope /subscriptions/$sub"
        fi
    done
    record INFO rbac "Owner coverage" "$ok of $n subscription(s)"
}

check_principals() {
    local i
    for ((i = 0; i < ${#P_IDS[@]}; i++)); do
        section "3.$((i + 1)) ${P_LABELS[$i]}: ${P_NAMES[$i]}"
        printf '         objectId %s%s\n' "${P_IDS[$i]}" "${P_APPIDS[$i]:+ | appId ${P_APPIDS[$i]}}"
        [[ "${P_TYPES[$i]}" == "ServicePrincipal" && -n "${P_APPIDS[$i]}" ]] && check_sp_health "${P_IDS[$i]}" "${P_APPIDS[$i]}"
        check_entra "${P_IDS[$i]}" "${P_TYPES[$i]}" "${P_LABELS[$i]} ${P_NAMES[$i]}"
        check_rbac  "${P_IDS[$i]}" "${P_TYPES[$i]}" "${P_LABELS[$i]} ${P_NAMES[$i]}"
    done
}

# -----------------------------------------------------------------------------
# 4. Subscription platform checks
# -----------------------------------------------------------------------------
check_provider_group() {  # sub providersJSON label ns...
    local sub="$1" providers="$2" label="$3" ns state unregistered=()
    shift 3
    for ns in "$@"; do
        state=$(jq -r --arg n "$ns" '.[] | select((.n|ascii_downcase)==($n|ascii_downcase)) | .s' <<<"$providers")
        if [[ "$state" != "Registered" ]]; then
            unregistered+=("$ns(${state:-NotFound})")
            remedy "az provider register --namespace $ns --subscription $sub"
        fi
    done
    if [[ ${#unregistered[@]} -eq 0 ]]; then
        record PASS platform "Providers - $label"
    else
        record WARN platform "Providers - $label" "Not registered: ${unregistered[*]}"
    fi
}

check_platform() {
    local i n sub name state providers diag count existing
    n=$(jq 'length' <<<"$SUBS_JSON")
    for ((i = 0; i < n; i++)); do
        sub=$(jq -r ".[$i].id" <<<"$SUBS_JSON"); name=$(jq -r ".[$i].name" <<<"$SUBS_JSON")
        state=$(jq -r ".[$i].state" <<<"$SUBS_JSON")
        section "4.$((i + 1)) Subscription: $name ($sub)"

        if [[ "$state" != "Enabled" ]]; then
            record FAIL platform "Subscription state" "State is '$state' (must be Enabled)."
            missing "Subscription $name not Enabled"
            continue
        fi
        record PASS platform "Subscription state" "Enabled"

        if providers=$(az provider list --subscription "$sub" \
                --query "[].{n:namespace, s:registrationState}" -o json 2>/dev/null); then
            check_provider_group "$sub" "$providers" "Activity Log" "${PROVIDERS_ACTIVITY_LOG[@]}"
            [[ $CHECK_AGENTLESS -eq 1 ]] && \
                check_provider_group "$sub" "$providers" "Agentless scanning" "${PROVIDERS_AGENTLESS[@]}"
        else
            record ERROR platform "Resource providers" "Unable to list resource providers."
        fi

        if diag=$(az monitor diagnostic-settings subscription list --subscription "$sub" -o json 2>/dev/null); then
            diag=$(jq -c 'if type=="array" then . else (.value // []) end' <<<"$diag")
            count=$(jq 'length' <<<"$diag")
            existing=$(jq -r '[.[].name | select(test("lacework|forticnapp"; "i"))] | join(", ")' <<<"$diag")
            if [[ "$count" -ge $DIAG_SETTINGS_MAX ]]; then
                record FAIL platform "Activity Log diagnostic settings" \
                    "$count/$DIAG_SETTINGS_MAX used - no free slot for the FortiCNAPP Activity Log export."
                missing "Free diagnostic-setting slot on $name"
            else
                record PASS platform "Activity Log diagnostic settings" "$count/$DIAG_SETTINGS_MAX used"
            fi
            [[ -n "$existing" ]] && record INFO platform "Existing FortiCNAPP/Lacework setting" \
                "$existing - an integration may already exist for this subscription."
        else
            record ERROR platform "Activity Log diagnostic settings" "Unable to list diagnostic settings."
        fi
    done
}

# -----------------------------------------------------------------------------
# Summary, report, exit
# -----------------------------------------------------------------------------
finish() {
    local status exit_code item report
    if   [[ $N_FAIL  -gt 0 ]]; then status="FAIL";               exit_code=1
    elif [[ $N_ERROR -gt 0 ]]; then status="UNABLE TO COMPLETE"; exit_code=2
    else                            status="PASS";               exit_code=0
    fi

    echo; banner
    printf '%s RESULT: %s%s\n' "$BLD" "$status" "$RST"
    banner
    printf '  Tenant        : %s\n' "${TENANT_ID:-n/a}"
    printf '  Console user  : %s\n' "${CALLER:-n/a}"
    printf '  Subscriptions : %s\n' "$(jq 'length' <<<"$SUBS_JSON")"
    printf '  Principals    : %s\n' "${#P_IDS[@]}"
    printf '  Checks        : %s%d pass%s  %s%d warn%s  %s%d fail%s  %s%d error%s\n' \
        "$GRN" "$N_PASS" "$RST" "$YEL" "$N_WARN" "$RST" "$RED" "$N_FAIL" "$RST" "$RED" "$N_ERROR" "$RST"

    if [[ ${#MISSING[@]} -gt 0 ]]; then
        printf '\n%sMissing / blocking:%s\n' "$BLD" "$RST"
        for item in "${MISSING[@]}"; do printf '  - %s\n' "$item"; done
    fi
    if [[ ${#REMEDIATION[@]} -gt 0 ]]; then
        printf '\n%sSuggested remediation (review first - requires admin rights):%s\n' "$BLD" "$RST"
        printf '%s\n' "${REMEDIATION[@]}" | awk '!seen[$0]++' | sed 's/^/  /'
    fi
    [[ "$status" == "PASS" ]] && printf '\n  All FortiCNAPP automated-integration prerequisites are present.\n'

    printf '\n%sLeast privilege:%s Owner + Privileged Role Administrator are highly\n' "$BLD" "$RST"
    printf '  privileged. Grant them for the onboarding window only (PIM / time-bound)\n'
    printf '  and remove them once the integration is verified.\n'

    local principals='[]' j
    for ((j = 0; j < ${#P_IDS[@]}; j++)); do
        principals=$(jq -c --arg id "${P_IDS[$j]}" --arg t "${P_TYPES[$j]}" --arg n "${P_NAMES[$j]}" \
            --arg a "${P_APPIDS[$j]}" --arg l "${P_LABELS[$j]}" \
            '. + [{objectId:$id, type:$t, name:$n, appId:$a, role:$l}]' <<<"$principals")
    done

    report="forticnapp-azure-preflight-$(date -u +%Y%m%dT%H%M%SZ).json"
    tmp_report="${report}.tmp.$$"
    if printf '%s\n' "${CHECKS_JSON[@]+"${CHECKS_JSON[@]}"}" | jq -s \
        --arg v "$VERSION" --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg status "$status" \
        --arg tenant "$TENANT_ID" --arg caller "$CALLER" --argjson subs "$SUBS_JSON" \
        --argjson principals "$principals" \
        --argjson missing "$(printf '%s\n' "${MISSING[@]+"${MISSING[@]}"}" | jq -R 'select(length>0)' | jq -s .)" \
        '{tool:"forticnapp-azure-preflight", version:$v, timestamp:$ts, status:$status,
          tenant:$tenant, consoleUser:$caller, subscriptions:$subs, principals:$principals,
          missing:$missing, checks:.}' \
        > "$tmp_report" 2>/dev/null && jq empty "$tmp_report" 2>/dev/null; then
        mv -f "$tmp_report" "$report"
        printf '\n  JSON report: %s\n' "$report"
    else
        rm -f "$tmp_report"
        printf '\n  %sERROR:%s failed to generate a valid JSON report (jq error). Re-run with '\''bash -x %s'\'' to see why.\n' \
            "$RED" "$RST" "$0"
    fi
    banner
    exit "$exit_code"
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
main() {
    case "${1:-}" in -h|--help) usage; exit 0 ;; -v|--version) echo "$VERSION"; exit 0 ;; esac

    banner
    printf '%s FortiCNAPP Azure Automated Integration - Preflight v%s%s\n' "$BLD" "$VERSION" "$RST"
    printf ' Zero-touch, read-only. Everything is discovered from your az session.\n'
    banner

    check_session
    discover_subscriptions
    discover_principals
    check_principals
    check_platform
    finish
}

main "$@"
