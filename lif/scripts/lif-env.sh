# Source me:  . ~/labzilla/lif/scripts/lif-env.sh   then:  local-ai doctor
_ip() { kubectl -n ai-system get svc "$1" -o jsonpath='{.spec.clusterIP}'; }
export LIF_CONTROLLER_URL="http://$(_ip controller):8080" LIF_GATEWAY_URL="http://$(_ip gateway):8080" \
       LIF_DECISION_URL="http://$(_ip decision-fabric):8080"
export LIF_ADMIN_KEY="$(cat ~/labzilla/secrets/lif-admin.key)" LIF_API_KEY="$(cat ~/labzilla/secrets/lif-operator.key)"
export PATH="$HOME/labzilla/lif/.venv/bin:$PATH"
unset -f _ip
