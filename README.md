# Gradescope Course Archiver

## Overview
This Python script downloads all your Gradescope course materials and creates individual Git repositories for each course, providing permanent access to your college work.

## Goals
- **One-time run**: Download everything once, create repos, done.
- **Handle UCLA SSO + 2FA**: Use session persistence to avoid repeated 2FA.
- **Organize by course**: Each course gets its own directory and git repo.
- **Automation**: Minimal manual intervention after initial login.
