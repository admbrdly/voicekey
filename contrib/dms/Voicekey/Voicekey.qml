import QtQuick
import Quickshell
import Quickshell.Io
import qs.Common
import qs.Services
import qs.Widgets
import qs.Modules.Plugins

PluginComponent {
    id: root
    layerNamespacePlugin: "voicekey"
    popoutWidth: 340

    property var status: ({state: "offline", listening: false})
    property string controlError: ""
    property bool requestPending: false
    property int requestId: 0
    property string serviceState: "unknown"
    property string serviceAction: ""
    property bool servicePending: false
    property bool serviceQueryAgain: false
    readonly property bool disabled: !online && serviceState === "inactive"
    readonly property bool starting: servicePending && serviceAction === "start"
    readonly property bool stopping: servicePending && serviceAction === "stop"
    readonly property bool canStart: idle || (online && status.state === "unloaded")
    readonly property bool controlsBusy: requestPending || servicePending
    readonly property bool online: control.linkUp && status.state !== "offline"
    readonly property bool listening: online && status.listening === true
    readonly property bool idle: online && status.state === "idle"
    readonly property bool muted: AudioService.source?.audio?.muted ?? false
    readonly property bool noMicrophone: !AudioService.source
    readonly property string stateText: {
        if (stopping) return "Disabling VoiceKey…";
        if (starting && !online) return "Starting VoiceKey…";
        if (disabled) return "VoiceKey disabled";
        if (!online) return "VoiceKey unavailable";
        if (listening && noMicrophone) return "Listening · no microphone";
        if (listening && muted) return "Listening · microphone muted";
        if (listening && status.models === "loading") return "Listening · loading models";
        if (listening) return "Listening";
        if (status.state === "finishing") return "Finishing dictation";
        if (status.state === "loading") return "Loading models";
        if (status.state === "unloading") return "Freeing memory…";
        if (status.state === "unloaded") return "Models unloaded · ready on next use";
        if (status.state === "unavailable") return "Dictation unavailable";
        return "Dictation off";
    }
    readonly property color stateColor: !online ? Theme.surfaceVariantText
        : listening ? ((muted || noMicrophone) ? Theme.error : Theme.primary)
        : status.state === "finishing" ? Theme.secondary : Theme.surfaceVariantText
    readonly property string stateIcon: !online ? "mic_off"
        : listening ? ((muted || noMicrophone) ? "mic_off" : "mic")
        : status.state === "finishing" ? "hourglass_top" : "mic_none"

    function send(command) {
        if (!control.linkUp || controlsBusy) return;
        requestPending = true;
        controlError = "";
        requestId++;
        control.send({command: command, id: requestId});
        requestTimeout.restart();
    }

    function checkService() {
        if (serviceQuery.running) serviceQueryAgain = true;
        else serviceQuery.running = true;
    }

    function setService(action) {
        if (servicePending) return;
        startAfterClose.stop();
        serviceAction = action;
        servicePending = true;
        serviceState = "unknown";
        controlError = "";
        serviceCommand.command = ["systemctl", "--user", action, "voicekey.service"];
        serviceCommand.running = true;
        serviceTimeout.restart();
    }

    Component.onCompleted: checkService()

    Process {
        id: serviceQuery
        command: ["systemctl", "--user", "show", "voicekey.service", "--property=ActiveState", "--value"]
        stdout: StdioCollector { id: serviceOutput }
        onExited: exitCode => {
            root.serviceState = exitCode === 0 ? serviceOutput.text.trim() : "unknown";
            if (root.serviceQueryAgain) {
                root.serviceQueryAgain = false;
                Qt.callLater(root.checkService);
            }
        }
    }
    Process {
        id: serviceCommand
        stderr: StdioCollector { id: serviceErrors }
        onExited: exitCode => {
            serviceTimeout.stop();
            root.servicePending = false;
            if (exitCode !== 0)
                root.controlError = serviceErrors.text.trim() || "Could not change VoiceKey service state";
            root.checkService();
        }
    }
    Timer {
        id: serviceTimeout
        interval: 35000
        onTriggered: {
            root.servicePending = false;
            root.controlError = "Service change not confirmed; check VoiceKey service status";
            if (serviceCommand.running) serviceCommand.signal(15);
            root.checkService();
        }
    }

    DankSocket {
        id: control
        path: Quickshell.env("XDG_RUNTIME_DIR") + "/voicekey/control.sock"
        connected: true
        onConnectionStateChanged: {
            if (!linkUp) {
                root.status = {state: "offline", listening: false};
                root.requestPending = false;
                requestTimeout.stop();
                root.serviceState = "unknown";
                root.checkService();
            }
        }
        parser: SplitParser {
            onRead: message => {
                try {
                    const data = JSON.parse(message);
                    if (data.type === "status") {
                        root.status = data;
                        heartbeat.restart();
                    } else if (data.type === "reply" && data.id === root.requestId) {
                        root.requestPending = false;
                        root.controlError = data.error || "";
                        requestTimeout.stop();
                    }
                } catch (e) {
                    root.controlError = "Invalid response from Voicekey";
                }
            }
        }
    }

    Timer {
        id: heartbeat
        interval: 6000
        onTriggered: {
            control.connected = false;
            Qt.callLater(() => { control.connected = true; });
        }
    }
    Timer {
        id: requestTimeout
        interval: 3000
        onTriggered: {
            root.requestPending = false;
            root.controlError = "Voicekey did not acknowledge the request";
        }
    }
    Timer {
        id: startAfterClose
        interval: 250
        onTriggered: root.send("start")
    }

    horizontalBarPill: Component {
        Row {
            spacing: Theme.spacingXS
            DankIcon {
                name: root.stateIcon
                size: root.iconSize
                color: root.stateColor
                anchors.verticalCenter: parent.verticalCenter
            }
            StyledText {
                visible: root.listening
                text: "Listening"
                color: root.stateColor
                font.pixelSize: Theme.fontSizeSmall
                anchors.verticalCenter: parent.verticalCenter
            }
        }
    }
    verticalBarPill: Component {
        DankIcon {
            name: root.stateIcon
            size: root.iconSize
            color: root.stateColor
        }
    }

    popoutContent: Component {
        PopoutComponent {
            id: panel
            headerText: "Voicekey"
            detailsText: root.stateText
            showCloseButton: true

            Column {
                width: parent.width
                spacing: Theme.spacingS

                StyledText {
                    width: parent.width
                    text: root.disabled ? "VoiceKey is stopped. Enable it here to use dictation again."
                        : root.status.destination || "Tap the dictation key to keep listening; hold to talk."
                    wrapMode: Text.Wrap
                    font.pixelSize: Theme.fontSizeSmall
                    color: Theme.surfaceVariantText
                }
                DankButton {
                    width: parent.width
                    text: root.listening ? "Stop listening" : "Start listening"
                    iconName: root.listening ? "stop" : "mic"
                    enabled: !root.controlsBusy && (root.listening || root.canStart)
                    onClicked: {
                        if (root.listening) root.send("stop");
                        else {
                            // Restore application focus before the daemon binds it.
                            panel.closePopout();
                            startAfterClose.restart();
                        }
                    }
                }
                DankToggle {
                    width: parent.width
                    text: "Follow focused window"
                    description: root.listening || root.status.state === "finishing"
                        ? "Stop dictation to change this."
                        : "Off pins the starting destination. Until Voicekey restarts."
                    checked: root.status.follow_focus === true
                    enabled: root.canStart && root.status.can_follow === true && !root.controlsBusy
                    onToggled: checked => root.send(checked ? "follow-focus" : "pin")
                }
                DankButton {
                    width: parent.width
                    text: root.status.unload_pending ? "Freeing memory…" : "Free memory"
                    iconName: "memory"
                    enabled: root.online && !root.controlsBusy && !root.status.unload_pending
                        && root.status.state !== "unloaded" && root.status.state !== "loading"
                    onClicked: root.send("free-memory")
                }
                StyledText {
                    width: parent.width
                    text: "Finishes pending speech and unloads models. The next dictation reloads them."
                    wrapMode: Text.Wrap
                    font.pixelSize: Theme.fontSizeSmall
                    color: Theme.surfaceVariantText
                }
                DankButton {
                    width: parent.width
                    text: root.stopping ? "Disabling…" : root.starting ? "Starting…"
                        : root.online || root.serviceState === "active" || root.serviceState === "activating"
                            ? "Disable VoiceKey" : "Enable VoiceKey"
                    iconName: "power_settings_new"
                    enabled: !root.servicePending && !serviceCommand.running
                    onClicked: root.setService(root.online || root.serviceState === "active"
                        || root.serviceState === "activating" ? "stop" : "start")
                }
                StyledText {
                    width: parent.width
                    text: "Disabling stops VoiceKey and its hotkeys for this login session. Other apps can still use the microphone."
                    wrapMode: Text.Wrap
                    font.pixelSize: Theme.fontSizeSmall
                    color: Theme.surfaceVariantText
                }
                StyledText {
                    width: parent.width
                    visible: root.muted || root.noMicrophone
                    text: root.noMicrophone ? "No microphone available" : "Microphone is muted"
                    color: Theme.error
                    font.pixelSize: Theme.fontSizeSmall
                }
                StyledText {
                    width: parent.width
                    visible: text.length > 0
                    text: root.controlError || root.status.error || ""
                    wrapMode: Text.Wrap
                    color: Theme.error
                    font.pixelSize: Theme.fontSizeSmall
                }
            }
        }
    }
}
