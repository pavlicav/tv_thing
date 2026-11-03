# /etc/nixos/tv-thing.nix: TV Thing on viviserv2, a second node (VIVISERV2_SETUP.md). Kept in the repo
# as deploy/nixos/tv-thing.nix; deploy/nixos/deploy-nixos.sh installs it and rebuilds.
# The video group, the services' Python, the three services (translations of
# deploy/systemd/*.service) and the front door on :8080 (deploy/apache/). This host's own settings
# are host_config.py's: auto from the hostname and hardware, plus ~/.config/tv-thing/host.json.
{ config, lib, pkgs, ... }:

let
  tv = "/home/vivian/tv_thing";

  # The services' and the fast suite's Python 3.13. It stands in for viviserv's miniconda.
  python = pkgs.python3.withPackages (ps: with ps; [
    numpy pillow scipy requests   # the services (scipy: sports_guide_music.py; requests: test_integration.py)
    pytest jsonschema             # the fast suite
  ]);

  # Pinned here, over the Config page and auto: nothing on this box may press a key on the house
  # Roku, which answers from anywhere on the LAN and belongs to viviserv and VizelVision.
  hostEnvironment = {
    TV_THING_ROKU = "off";
    # Each line reaches the journal when printed, not when an 8 KiB buffer fills.
    PYTHONUNBUFFERED = "1";
  };
  unit = {
    wantedBy = [ "multi-user.target" ];
    path = [ pkgs.ffmpeg pkgs.procps ];   # NixOS units get a minimal PATH; the code runs `ffmpeg`/`ffprobe`
  };
  service = {
    Type = "simple";
    User = "vivian";
    WorkingDirectory = tv;
    Restart = "always";
    TimeoutStopSec = 5;
  };
in
{
  # /dev/dvb/adapter*/* are root:video 0660.
  users.users.vivian.extraGroups = [ "video" ];

  # The UHD 630's VA-API driver (iHD), for the feed's h264_vaapi encoder: libva finds it under
  # /run/opengl-driver. The gate passed on it, both tuners at once (VIVISERV2_SETUP.md, section 3).
  # Its packagers trip at 90 C on the CPU package's sensor, the iGPU being on the CPU's die.
  hardware.graphics = {
    enable = true;
    extraPackages = [ pkgs.intel-media-driver ];
  };

  environment.systemPackages = [
    python
    pkgs.tesseract   # only the fast suite's Roku OCR tests use it on this host
    pkgs.libva-utils # vainfo: whether the iGPU encoder is there
  ];

  systemd.services.tv-thing-feed = unit // {
    description = "TV Thing feed (tuners, recorders, streams)";
    wants = [ "network-online.target" ];
    after = [ "network-online.target" ];
    # A rebuild must never restart the feed behind your back: that drops every stream and ends
    # every recording. Restart it yourself, deliberately.
    restartIfChanged = false;
    environment = hostEnvironment // {
      TV_THING_VIDEO_ENCODER = "vaapi";   # h264_vaapi on the iGPU; "cpu" for libx264
      LIBVA_DRIVER_NAME = "iHD";   # the UHD 630's driver, not the older i965
      TV_THING_CONTROL_URL = "http://127.0.0.1:8087";
    };
    serviceConfig = service // {
      ExecStart = "${python}/bin/python3 ${tv}/feed_server.py";
      RestartSec = 5;
      CPUQuota = "400%";      # viviserv: 800% of 28 threads. Here: 4 of 6 faster cores
      MemoryMax = "1800M";
      TasksMax = 192;
      OOMPolicy = "continue";
      LimitFSIZE = "16M:16G";
    };
  };

  systemd.services.tv-thing = unit // {
    description = "TV Thing control (API, UI, channels, guides, VCR)";
    wants = [ "network-online.target" "tv-thing-feed.service" ];
    after = [ "network-online.target" "tv-thing-feed.service" ];
    environment = hostEnvironment // {
      TV_THING_ROLE = "control";
      PORT = "8087";
      TV_THING_FEED_URL = "http://127.0.0.1:8086/";
    };
    serviceConfig = service // {
      ExecStart = "${python}/bin/python3 ${tv}/server.py";
      RestartSec = 2;
      CPUQuota = "200%";      # viviserv: 400%
      MemoryMax = "1200M";
      TasksMax = 160;
      OOMPolicy = "stop";
      LimitFSIZE = "16M:16G";
    };
  };

  systemd.services.tv-thing-web-basic = unit // {
    description = "TV Thing web-basic (slide channels' video, simple web relays)";
    wants = [ "network-online.target" ];
    after = [ "network-online.target" ];
    environment = hostEnvironment // {
      TV_THING_CONTROL_URL = "http://127.0.0.1:8087";
    };
    serviceConfig = service // {
      ExecStart = "${python}/bin/python3 ${tv}/web_basic.py";
      RestartSec = 5;
      CPUQuota = "200%";      # as viviserv: web_stream_check.py's own peak limit
      MemoryMax = "768M";
      TasksMax = 96;
      LimitFSIZE = "16M:2G";
    };
  };

  # The front door: Apache on :8080. /stream/ goes to the feed (and web-basic's paths to
  # web-basic), everything else to control, and /internal/ and /feed/v1/ are never routed.
  # It joins the dashboard's Apache (viviserv2-dashboard.nix), which already sets adminAddr and
  # loads proxy, proxy_http, headers and alias. *:8080, not explicit addresses: viviserv binds
  # its own only because another service holds tailscale0's :8080 there.
  # The routes are the repo's, read at rebuild time, so a change to them takes effect at the
  # next `nixos-rebuild`, not on a pull.
  services.httpd.virtualHosts.tv-thing-front-door = {
    listen = [ { port = 8080; } ];
    extraConfig = ''
      Define TV_THING_FEED_PORT 8086
      Define TV_THING_CONTROL_PORT 8087
      Define TV_THING_PEER_URL http://10.0.0.98:8080
    '' + builtins.readFile "${tv}/deploy/apache/tv-thing-routes.conf";
  };
  # TV Thing at http://viviserv2/tv/ too, on the dashboard's site, as viviserv's main site has it:
  # the dashboard's TV Thing card links there. A <Location>, not viviserv's vhost-level ProxyPass:
  # the dashboard proxies <Location />, and mod_proxy tries a Location's ProxyPass before the
  # vhost's, so a vhost-level /tv/ would never match. Locations are written in name order, so
  # /tv/ follows / and wins for its paths. flushpackets for the rescan's events and HLS; the
  # timeout so a scan is not cut off. The bare /tv redirects by mod_rewrite, which translates
  # before mod_proxy (a RedirectMatch would lose /tv to the dashboard's proxy).
  services.httpd.virtualHosts.viviserv2 = {
    extraConfig = ''
      RewriteEngine On
      RewriteRule ^/tv$ /tv/ [R=301,L]
    '';
    locations."/tv/".extraConfig = ''
      ProxyPass http://127.0.0.1:8080/ flushpackets=on timeout=3600
      ProxyPassReverse http://127.0.0.1:8080/
    '';
  };
  systemd.services.httpd = {
    # NixOS restarts Apache whenever its config changes, which drops every viewer. Reload it
    # instead: its ExecReload is `httpd -k graceful`.
    restartTriggers = lib.mkForce [ ];
    reloadTriggers = [ config.services.httpd.configFile ];
  };

  networking.firewall.allowedTCPPorts = [ 8080 ];
}
