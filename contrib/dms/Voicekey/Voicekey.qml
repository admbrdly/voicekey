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
    readonly property bool online: control.linkUp && status.state !== "offline"
    readonly property bool listening: online && status.listening === true
    readonly property bool idle: online && status.state === "idle"
    readonly property bool muted: AudioService.source?.audio?.muted ?? false
    readonly property bool noMicrophone: !AudioService.source
    readonly property string stateText: {
        if (!online) return "Voicekey offline";
        if (listening && noMicrophone) return "Listening · no microphone";
        if (listening && muted) return "Listening · microphone muted";
        if (listening) return "Listening";
        if (status.state === "finishing") return "Finishing dictation";
        if (status.state === "loading") return "Loading models";
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
        if (!control.linkUp || requestPending) return;
        requestPending = true;
        controlError = "";
        requestId++;
        control.send({command: command, id: requestId});
        requestTimeout.restart();
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
                    text: root.status.destination || "Tap the dictation key to keep listening; hold to talk."
                    wrapMode: Text.Wrap
                    font.pixelSize: Theme.fontSizeSmall
                    color: Theme.surfaceVariantText
                }
                DankButton {
                    width: parent.width
                    text: root.listening ? "Stop listening" : "Start listening"
                    iconName: root.listening ? "stop" : "mic"
                    enabled: !root.requestPending && (root.listening || root.idle)
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
                    enabled: root.idle && root.status.can_follow && !root.requestPending
                    onToggled: checked => root.send(checked ? "follow-focus" : "pin")
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
