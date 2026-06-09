# Linux box with a real desktop-notification stack (dbus + dunst daemon + notify-send + a virtual
# display), used to verify the background agent and the native toast end to end inside a container.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    dbus dbus-x11 dunst libnotify-bin xvfb curl procps && rm -rf /var/lib/apt/lists/*
WORKDIR /work
