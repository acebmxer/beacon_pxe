---
name: Bug report
about: Something in Beacon does not work as documented
title: '[Bug] '
labels: bug
assignees: ''
---

## What happened

<!-- What you did and what you saw. Paste error messages exactly. -->

## What you expected

## Steps to reproduce

1. Open Beacon and go to `...`
2. Click / boot `...`
3. See error

## Logs

<!-- `docker compose logs <service>` for the affected container (web, nginx,
     dnsmasq, nfs, smb), with anything sensitive removed. -->

```
(paste output here)
```

## Environment

| Field | Value |
|-------|-------|
| Beacon version | <!-- the "Running ... channel" chip at the top of the web UI, or `docker inspect beacon_web --format '{{.Config.Image}}'` --> |
| Host OS | <!-- e.g. Fedora 45, Ubuntu 26.04 --> |
| Docker | <!-- output of `docker --version` and `docker compose version` --> |
| New install or upgrade | <!-- and from which version --> |
| DHCP mode | <!-- see the README's DHCP modes section --> |
| Client & image | <!-- e.g. Dell OptiPlex UEFI booting Windows 11, a VM booting Fedora 45 --> |

## Where does it fail?

- [ ] In the web UI
- [ ] Before the boot menu shows (DHCP / TFTP)
- [ ] In the boot menu or while the image loads
- [ ] In the installer or live system after it starts
- [ ] Not sure

## Additional context

<!-- Screenshots, a photo of the client's screen, or anything else that might
     help. Check docs/troubleshooting.md first. -->
