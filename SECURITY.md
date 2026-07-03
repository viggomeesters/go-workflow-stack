# Security Policy

## Supported versions

This repository is an early public spike. Security fixes are handled on `main`.

## Reporting a vulnerability

Please do not open public issues for secrets, credential leaks, or exploitable behavior. Contact the repository owner through GitHub with a private vulnerability report.

## Data boundary

This project must not contain private vault data, customer data, credentials, tokens, or machine-local runtime state. The `.go/` examples are synthetic template state only.
