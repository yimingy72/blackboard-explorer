# Only the existing administrator relay receives the verified native UI build.
# The AgentGateway deployment, binary, image and store are not rebuilt.
ARG BBX_RELAY_BASE
FROM ${BBX_RELAY_BASE}
COPY admin_ui_proxy.py /opt/bbx-native-ui-relay/admin_ui_proxy.py
COPY assets-manifest.json /opt/bbx-native-ui-bundle/assets-manifest.json
COPY dist/ /opt/bbx-native-ui-bundle/dist/
ENV BBX_NATIVE_LOCALIZED_UI_DIR=/opt/bbx-native-ui-bundle
