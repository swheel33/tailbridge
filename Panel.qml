import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "swheel33.tailbridge"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  property var service: null
  property int phraseIndex: 0

  readonly property var activePhrases: [
    "Bridging clipboards",
    "Shuttling snippets",
    "Ferrying files",
    "Passing pixels",
    "Carrying copies",
    "Tunneling text",
    "Moving media",
    "Packing pasteboards"
  ]
  readonly property string activePhrase: activePhrases[phraseIndex % activePhrases.length]

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property bool showingQr: service && service.qrSize > 0 && service.qrError === ""
  readonly property string qrTitle: service && service.qrKind === "claim" ? "Copy to iPhone" : "Set up iPhone"
  readonly property string qrMeta: !service ? ""
    : service.qrKind === "install" ? "Step 1 of 2"
    : service.qrKind === "setup" ? "Step 2 of 2"
    : "Clipboard transfer"
  readonly property string qrSection: !service ? ""
    : service.qrKind === "install" ? "INSTALL THE SHORTCUT"
    : service.qrKind === "setup" ? "CONNECT THIS COMPUTER"
    : "SCAN WITH CAMERA"
  readonly property string qrInstruction: !service ? ""
    : service.qrKind === "install" ? "Scan this code with Camera, then tap Add Shortcut."
    : service.qrKind === "setup" ? "Scan this code with Camera and wait for the Tailbridge configured notification."
    : "Scan this code with Camera to copy your Omarchy clipboard."

  function open() {
    controller.show()
  }

  function close() {
    controller.hide()
    if (service) service.dismissQr()
  }

  function switchPanel(direction) {
    if (bar && typeof bar.switchPanelFrom === "function")
      return bar.switchPanelFrom(hostWidget || root, direction)
    return false
  }

  function goBack() {
    if (!service) return
    if (service.qrKind === "setup") service.requestInstall()
    else service.dismissQr()
  }

  onShowingQrChanged: if (showingQr) {
    phraseSwap.stop()
    headerMeta.opacity = 1.0
  }

  component ActionRow: CursorSurface {
    id: actionRow
    property string iconText: ""
    property string title: ""
    property string subtitle: ""
    property string actionIcon: "󰅂"
    signal clicked()

    width: parent ? parent.width : 0
    foreground: root.foreground
    hasCursor: enabled && actionMouse.containsMouse
    implicitHeight: actionContent.implicitHeight + Style.spacing.rowPaddingX
    opacity: enabled ? 1 : 0.45

    MouseArea {
      id: actionMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: actionRow.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
      onClicked: if (actionRow.enabled) actionRow.clicked()
    }

    RowLayout {
      id: actionContent
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(8)
      spacing: Style.space(8)

      Text {
        text: actionRow.iconText
        visible: text !== ""
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.icon
        horizontalAlignment: Text.AlignHCenter
        Layout.preferredWidth: Style.space(22)
        Layout.alignment: Qt.AlignVCenter
      }

      ColumnLayout {
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          text: actionRow.title
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
          Layout.fillWidth: true
        }

        Text {
          text: actionRow.subtitle
          visible: text !== ""
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
          Layout.fillWidth: true
        }
      }

      Text {
        text: actionRow.actionIcon
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.icon
        Layout.alignment: Qt.AlignVCenter
      }
    }
  }

  PopupCard {
    id: popup
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    contentWidth: popup.fittedContentWidth(Style.space(380))
    contentHeight: popup.fittedContentHeight(content.implicitHeight, Style.space(560))

    Column {
      id: content
      width: parent.width
      spacing: Style.space(12)

      Item {
        id: header
        width: parent.width
        implicitHeight: Math.max(headerIcon.implicitHeight, headerLabels.implicitHeight, qrBackButton.height)

        Text {
          id: headerIcon
          visible: !root.showingQr
          text: service && service.configured ? "󰅍" : "󰨸"
          color: root.foreground
          opacity: service && service.phase === "ready" ? 1.0 : 0.5
          font.family: root.fontFamily
          font.pixelSize: Style.font.display
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
        }

        Column {
          id: headerLabels
          anchors.left: headerIcon.right
          anchors.leftMargin: Style.space(14)
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          spacing: Style.space(2)

          Text {
            text: root.showingQr ? root.qrTitle : "Tailbridge"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
            elide: Text.ElideRight
            width: parent.width
          }

          Text {
            id: headerMeta
            text: (root.showingQr ? root.qrMeta
              : !service ? "Service unavailable"
              : service.phase === "error" ? service.detail
              : service.phase === "starting" ? "Starting"
              : service.configured ? root.activePhrase
              : "iPhone setup required").toUpperCase()
            color: Qt.darker(root.foreground, 1.4)
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
            font.letterSpacing: 1.2
            elide: Text.ElideRight
            width: parent.width
          }
        }

        CursorSurface {
          id: qrBackButton
          visible: root.showingQr
          width: Style.space(28)
          height: Style.space(28)
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
          foreground: root.foreground
          hasCursor: backMouse.containsMouse

          Text {
            anchors.centerIn: parent
            text: "\u2039"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.title
          }

          MouseArea {
            id: backMouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: root.goBack()
          }

          PanelToolTip {
            visible: backMouse.containsMouse
            text: "Back"
            fontFamily: root.fontFamily
          }
        }
      }

      Text {
        visible: !root.showingQr && service && service.qrBusy
        width: parent.width
        text: "Generating QR code..."
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        horizontalAlignment: Text.AlignHCenter
      }

      Text {
        visible: !root.showingQr && service && service.qrError !== ""
        width: parent.width
        text: service ? service.qrError : ""
        color: root.urgent
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.WordWrap
      }

      PanelSeparator {
        foreground: root.foreground
      }

      Column {
        visible: root.showingQr
        width: parent.width
        spacing: Style.space(10)

        PanelSectionHeader {
          text: root.qrSection
          foreground: root.foreground
          fontFamily: root.fontFamily
        }

        Text {
          width: parent.width
          text: root.qrInstruction
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.WordWrap
        }

        Rectangle {
          id: qrCanvas
          readonly property int moduleSize: service && service.qrSize > 0
            ? Math.max(4, Math.floor(Style.space(220) / service.qrSize)) : 0
          readonly property int matrixSize: service ? service.qrSize * moduleSize : 0
          width: matrixSize
          height: matrixSize
          anchors.horizontalCenter: parent.horizontalCenter
          color: "white"
          radius: Style.cornerRadius

          Grid {
            width: qrCanvas.matrixSize
            height: qrCanvas.matrixSize
            columns: service ? service.qrSize : 0

            Repeater {
              model: service ? service.qrSize * service.qrSize : 0
              Rectangle {
                required property int index
                readonly property int row: Math.floor(index / service.qrSize)
                readonly property int column: index % service.qrSize
                width: qrCanvas.moduleSize
                height: qrCanvas.moduleSize
                color: service.qrRows[row].charAt(column) === "1" ? "#111111" : "transparent"
              }
            }
          }
        }

        Item {
          visible: service && service.qrKind === "claim"
          width: parent.width
          implicitHeight: Math.max(claimExpiry.implicitHeight, refreshCode.implicitHeight)

          Text {
            id: claimExpiry
            anchors.left: parent.left
            text: "Expires in 5 minutes"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Text {
            id: refreshCode
            anchors.right: parent.right
            text: service && service.qrBusy ? "Refreshing..." : "Refresh code"
            color: refreshMouse.containsMouse && refreshMouse.enabled ? root.foreground : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption

            MouseArea {
              id: refreshMouse
              anchors.fill: parent
              enabled: service && !service.qrBusy
              hoverEnabled: true
              cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
              onClicked: service.requestClipboard()
            }
          }
        }

        ActionRow {
          visible: service && service.qrKind === "install"
          title: "Continue"
          subtitle: "After the Shortcut is installed"
          enabled: service && !service.qrBusy
          onClicked: service.requestSetup()
        }

        ActionRow {
          visible: service && service.qrKind === "setup"
          title: "Finish setup"
          subtitle: "After Tailbridge confirms the connection"
          enabled: service && service.ready && !service.qrBusy
          onClicked: service.completeSetup()
        }
      }

      Column {
        visible: !root.showingQr && service && service.configured
        width: parent.width
        spacing: Style.space(10)

        PanelSectionHeader {
          text: "CLIPBOARD"
          foreground: root.foreground
          fontFamily: root.fontFamily
        }

        ActionRow {
          title: "Copy to iPhone"
          subtitle: "Create a QR code for your current clipboard"
          actionIcon: "󰒊"
          enabled: service && service.ready && !service.qrBusy
          onClicked: service.requestClipboard()
        }
      }

      Column {
        visible: !root.showingQr && service && !service.configured
        width: parent.width
        spacing: Style.space(10)

        PanelSectionHeader {
          text: "SETUP"
          foreground: root.foreground
          fontFamily: root.fontFamily
        }

        ActionRow {
          iconText: "󰐕"
          title: "Set up iPhone"
          subtitle: "Install and connect the Tailbridge Shortcut"
          enabled: service && service.ready && !service.qrBusy
          onClicked: service.requestInstall()
        }
      }

      Item {
        visible: !root.showingQr && service && service.configured
        width: parent.width
        implicitHeight: setupRow.implicitHeight

        Row {
          id: setupRow
          anchors.left: parent.left
          anchors.leftMargin: Style.space(10)
          spacing: Style.space(6)

          Text {
            text: "+"
            color: setupMouse.containsMouse ? root.foreground : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Text {
            text: "Set up another iPhone"
            color: setupMouse.containsMouse ? root.foreground : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        MouseArea {
          id: setupMouse
          anchors.fill: setupRow
          hoverEnabled: true
          cursorShape: Qt.PointingHandCursor
          onClicked: service.requestInstall()
        }
      }

    }
  }

  Timer {
    id: phraseTimer
    interval: 2800
    running: root.opened && !root.showingQr && service && service.ready && service.configured
    repeat: true
    onTriggered: phraseSwap.restart()
  }

  SequentialAnimation {
    id: phraseSwap
    PropertyAnimation {
      target: headerMeta
      property: "opacity"
      to: 0.0
      duration: 180
      easing.type: Easing.OutQuad
    }
    ScriptAction {
      script: root.phraseIndex = (root.phraseIndex + 1) % root.activePhrases.length
    }
    PropertyAnimation {
      target: headerMeta
      property: "opacity"
      to: 1.0
      duration: 260
      easing.type: Easing.InQuad
    }
  }
}
