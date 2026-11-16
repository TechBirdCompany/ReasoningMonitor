# TechBird - Reasoning Monitor

A lightweight, self-hosted monitoring tool for observing and inspecting reasoning output from OpenAI-compatible LLM APIs.

The TechBird - Reasoning Monitor acts as a passive reverse proxy between an API client (such as Visual Studio Code with GitHub Copilot) and an OpenAI-compatible model server. It captures reasoning content and final model responses in real time and presents them in a web-based dashboard.

The application is designed to make otherwise hidden model reasoning, request activity, and generation performance easier to inspect.

---

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [VS Code Integration](#vs-code-integration)
- [Dashboard](#dashboard)
- [Request Processing](#request-processing)
- [Database and Data Retention](#database-and-data-retention)
- [Token Counting and Performance](#token-counting-and-performance)
- [Security and Privacy](#security-and-privacy)
- [Command-Line Options](#command-line-options)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)
- [Development](#development)
- [AI Authorship and Disclaimer](#ai-authorship-and-disclaimer)
- [License](#license)

## Overview

Many modern language models support dedicated reasoning or thinking output. Depending on the model and API implementation, this information may be returned separately from the final response.

However, many API clients do not expose this information in an easily accessible way.

The TechBird - Reasoning Monitor addresses this by intercepting API traffic without changing the actual requests or responses.

It provides:

- Real-time visibility into model reasoning.
- Separate presentation of reasoning and final answers.
- Monitoring of multiple requests.
- Request and generation statistics.
- Persistent request history.
- Automatic cleanup of old data.

**Important:** This application does not generate, schedule, modify, or independently request anything from the model. It only observes requests sent by an API client configured to use the proxy.

## Features

### Passive OpenAI-compatible reverse proxy

- Forwards API requests to a configurable upstream server.
- Supports HTTP and HTTPS upstream connections.
- Preserves the original request body and response stream.
- Supports streaming and non-streaming responses.
- Handles concurrent requests.
- Supports common HTTP methods, including GET, POST, PUT, PATCH and DELETE.

### Reasoning capture

The monitor recognizes the following reasoning fields:

- `reasoning_content`
- `reasoning`

It also captures the regular response content from:

- `content`

Both streamed Server-Sent Events (SSE) and regular JSON responses are supported.

### Live dashboard

A web-based dashboard provides an overview of current and historical model activity.

### Request statistics

The dashboard provides information about:

- Requests processed today.
- Currently active requests.
- Generated tokens or estimated streamed chunks.
- Average generation throughput.

### Persistent history

Completed requests are stored in a local SQLite database.

### Automatic retention

- Completed requests are retained for seven days.
- Expired entries are removed at startup.
- A background cleanup worker periodically removes expired entries.
- Active requests are not removed while they are running.

### Manual history reset

The dashboard includes a reset function with a confirmation dialog.

It permanently deletes the stored request history and resets the displayed statistics.

## Architecture

The application consists of three main components:

1. API reverse proxy
2. Request storage
3. NiceGUI web dashboard

### Request flow

```text
+-----------------------+
|     VS Code / Client  |
|                       |
|   GitHub Copilot      |
+-----------+-----------+
            |
            | OpenAI-compatible API
            v
+-----------------------+
|   Reasoning Monitor   |
|                       |
|  Reverse Proxy :4001  |
|                       |
|  - Request forwarding |
|  - SSE inspection     |
|  - Reasoning capture  |
|  - Response capture   |
+-----------+-----------+
            |
            | Original API request
            v
+-----------------------+
|    LLM API Server     |
|                       |
|    llama.cpp          |
|    or compatible API  |
+-----------------------+

            |
            v
+-----------------------+
|     RequestStore      |
|                       |
|   In-memory storage   |
|   SQLite persistence  |
+-----------+-----------+
            |
            v
+-----------------------+
|    NiceGUI Dashboard  |
|                       |
|       Port 8088       |
+-----------------------+
```

The proxy forwards response data to the original client while independently extracting relevant reasoning and response information.

The dashboard reads from the shared request store and updates automatically.

### Technology stack

| Component | Technology |
|---|---|
| Programming language | Python |
| HTTP proxy | Python standard library |
| Web framework | NiceGUI |
| Database | SQLite |
| Streaming protocol | Server-Sent Events (SSE) |
| API compatibility | OpenAI-compatible APIs |

## Requirements

- Linux, Windows or another platform supporting Python.
- Python 3.10 or newer.
- Network access to the upstream LLM API.
- An OpenAI-compatible API endpoint.
- A supported Python environment.

Python dependencies:

```text
nicegui>=2.0,<4.0
```

No external database server is required.

## Installation

### 1. Clone the repository

```bash
git clone <repository-url>
cd reasoning_monitor
```

Alternatively, download the project files and extract them into a directory of your choice.

### 2. Create a virtual environment

```bash
python3 -m venv .venv
```

Activate it:

```bash
source .venv/bin/activate
```

### 3. Install dependencies

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements_reasoning_monitor.txt
```

### 4. Start the application

Example configuration:

```bash
python reasoning_monitor.py \
  --upstream http://10.10.10.22:4000 \
  --proxy-host 0.0.0.0 \
  --proxy-port 4001 \
  --web-host 0.0.0.0 \
  --web-port 8088
```

After startup, the application prints its configuration and service addresses.

Open the dashboard:

```text
http://<server-ip>:8088
```

The API proxy is available at:

```text
http://<server-ip>:4001/v1
```

## Configuration

The upstream API endpoint defines where requests are forwarded.

Example:

```text
http://10.10.10.22:4000
```

The monitor itself does not require a specific model or model identifier.

It reads the model identifier from the incoming API request.

### Configuration parameters

| Parameter | Default | Description |
|---|---|---|
| `--proxy-host` | `0.0.0.0` | Proxy bind address |
| `--proxy-port` | `4001` | Proxy listening port |
| `--upstream` | `http://10.10.10.22:4000` | Upstream API base URL |
| `--web-host` | `0.0.0.0` | Dashboard bind address |
| `--web-port` | `8088` | Dashboard listening port |
| `--db` | `./reasoning_monitor.sqlite3` | SQLite database location |
| `--timeout` | `600` | Upstream timeout in seconds |

To see all available options:

```bash
python reasoning_monitor.py --help
```

## VS Code Integration

The monitor can be used with Visual Studio Code and GitHub Copilot when using a configurable OpenAI-compatible model endpoint.

The important part is to change the endpoint of the desired model so that requests pass through the monitoring proxy.

For example, the original endpoint might be:

```json
"url": "http://10.10.10.22:4000/v1"
```

Change it to:

```json
"url": "http://10.10.10.22:4001/v1"
```

Keep the existing model identifier and other model configuration parameters unchanged.

This includes:

- Model ID
- Tool calling configuration
- Vision capabilities
- Token limits
- Other model-specific settings

Only the endpoint needs to be redirected.

Other models can continue communicating directly with their original endpoints.

**Note:** The exact configuration location depends on the VS Code extension and its version. The example above assumes a configurable `chatLanguageModels.json` entry.

## Dashboard

The dashboard is designed to provide a live overview of model activity.

### Statistics

The top section displays:

**Requests Today**

The number of completed and currently active requests started today.

**Active Requests**

The number of requests currently being processed.

**Tokens Today**

The total number of generated completion tokens, when available, or an estimate based on streamed output chunks.

**Average Throughput**

The average generation speed across the relevant requests.

### Captured Requests

Each request is displayed as an individual entry.

The displayed information includes:

- Local request timestamp.
- Model identifier.
- Client IP address.
- Requested API endpoint.
- Current request status.
- HTTP response status, when available.
- Generation throughput.
- Token count or estimated streamed chunks.

### Thinking

The Thinking section contains captured reasoning output.

It updates while the model is generating its response.

### Final Response

The Final Response section contains the regular model response.

Markdown formatting is supported by the dashboard.

### Automatic updates

The dashboard refreshes its displayed information every 100 milliseconds.

The newest requests are displayed at the top.

The dashboard displays up to 50 request cards at a time.

## Request Processing

The monitor distinguishes between two types of API responses.

### Streaming responses

For streaming responses, the proxy inspects Server-Sent Events.

Relevant information is extracted from the JSON payloads while the original response data is forwarded to the client.

The monitor does not need to wait for the complete model response before displaying reasoning.

### Non-streaming responses

For regular JSON responses, the monitor extracts reasoning and final response content from the response message.

### Request status

Requests are tracked throughout their lifecycle.

The dashboard distinguishes between:

- `LIVE` – Request is currently being processed.
- `COMPLETE` – Request completed successfully.
- Error or failed states – Request did not complete normally.

## Database and Data Retention

The monitor uses SQLite for persistent request history.

The default database filename is:

```text
reasoning_monitor.sqlite3
```

The database is created automatically when the application starts.

### Stored information

The database contains:

- Request ID.
- Timestamp.
- Model identifier.
- API endpoint.
- Client IP address.
- Request status.
- HTTP status code.
- Captured reasoning.
- Final response.
- Streamed chunk counts.
- Prompt token count, if supplied.
- Completion token count, if supplied.
- Total token count, if supplied.
- Generation duration.
- Error information.

The original incoming request body, including the user's prompt, is not persisted as part of the request history.

### In-memory storage

The application maintains a limited in-memory request history for active dashboard updates.

- Maximum in-memory requests: 80.
- Maximum visible dashboard cards: 50.
- Maximum stored reasoning text per request: 500,000 characters.
- Maximum stored response text per request: 500,000 characters.

Older entries are removed from the in-memory collection when the limit is exceeded.

Completed requests remain available in SQLite until the retention policy removes them.

### Automatic cleanup

The default retention period is seven days.

Cleanup takes place:

- During application startup.
- Periodically in the background, approximately once per hour.

Only completed historical entries are removed by the retention mechanism.

### Manual reset

The dashboard provides a Reset History action.

After confirmation, the application deletes all stored history and clears its in-memory request collection.

This operation cannot be undone.

## Token Counting and Performance

The monitor attempts to display the actual completion token count when the upstream API provides usage information.

It recognizes:

- `prompt_tokens`
- `completion_tokens`
- `total_tokens`

If `completion_tokens` is unavailable, the monitor estimates generated output based on the number of non-empty streamed reasoning and response chunks.

### Important accuracy limitation

The fallback value is not an exact tokenizer-based token count.

A single SSE event may contain:

- Part of a token.
- Multiple tokens.
- A complete text fragment.
- An arbitrary amount of generated text.

Consequently, the estimated value should be treated as an indicator of activity rather than an authoritative token count.

The same limitation applies to throughput calculations when actual usage information is unavailable.

When actual completion token usage is supplied, it takes precedence over the estimate.

## Security and Privacy

The monitor is intended for private and trusted environments.

### Network exposure

By default, both services bind to:

```text
0.0.0.0
```

This means they listen on all available network interfaces.

The default ports are:

| Service | Port |
|---|---|
| API Proxy | 4001 |
| Web Dashboard | 8088 |

### Authentication

The dashboard currently has no built-in authentication.

Anyone who can access the dashboard may be able to inspect captured reasoning, responses and request metadata.

Do not expose the dashboard directly to the public internet.

Restrict access through firewall rules or a trusted private network.

### Stored data

Captured reasoning and final responses are stored locally in SQLite.

Although original user prompts are not persisted, reasoning and responses may contain sensitive information.

Protect the database file and its containing directory accordingly.

## Troubleshooting

### Dashboard is unavailable

Check whether the application is running and whether the configured web port is accessible.

```bash
ss -tulpn | grep 8088
```

Check firewall rules if the application is reachable locally but not from another machine.

### API requests are not visible

Verify that the client is using the proxy endpoint:

```text
http://<server-ip>:4001/v1
```

Check that the proxy can reach the upstream API.

### No reasoning is displayed

Possible reasons include:

- The model does not generate separate reasoning output.
- The upstream API does not expose reasoning fields.
- The model uses a different reasoning field format.
- The API client or server filters reasoning information.
- The response does not contain `reasoning_content` or `reasoning`.

The monitor cannot display information that the upstream API does not provide.

### Final responses are visible, but reasoning is missing

This usually indicates that the upstream server is returning regular response content but not exposing its reasoning output.

Check the model configuration and the upstream API response format.

### Token counts appear inaccurate

Check whether the upstream API supplies completion usage information.

If it does not, the monitor uses streamed chunk counts as an estimate.

### Database errors

Check whether the application has write permissions for the database directory.

The database is created automatically if it does not exist.

## Limitations

The current implementation has several intentional limitations:

- The monitor only observes requests routed through it.
- It does not independently query or control the model.
- It cannot recover reasoning that the upstream API does not expose.
- Token counts may be estimates rather than exact tokenizer counts.
- The dashboard does not provide built-in user authentication.
- Request history is limited to seven days by default.
- Only the most recent 50 entries are displayed simultaneously.
- The application does not provide a complete archive of original user prompts.

The monitor is designed primarily as a lightweight inspection and debugging tool rather than a complete API analytics platform.

## Development

The project is intentionally kept lightweight.

The main application is contained in a single Python file:

```text
reasoning_monitor/
├── reasoning_monitor.py
├── requirements_reasoning_monitor.txt
├── .gitignore
├── README.md
└── reasoning_monitor.sqlite3   # Generated automatically
```

The SQLite database is runtime data and should not be committed to version control.

### Starting in development

Activate the virtual environment:

```bash
source .venv/bin/activate
```

Start the application:

```bash
python reasoning_monitor.py
```

For available configuration options:

```bash
python reasoning_monitor.py --help
```

## AI Authorship and Disclaimer

**The application code was written by AI.**

The project was developed through AI-assisted code generation, with human direction, configuration, testing and iterative refinement.

The implementation, including its proxy logic, database handling, request processing and dashboard, was generated with the assistance of artificial intelligence.

The code should therefore be considered AI-generated software rather than a traditionally hand-written software project.

AI-generated code is not guaranteed to be free of bugs, security vulnerabilities or implementation errors.

Users are encouraged to:

- Review the source code before deployment.
- Verify its behavior in their own environment.
- Test changes before using them in production.
- Apply appropriate network and data security measures.

The author does not claim that every implementation detail has been independently audited or formally verified.

## License and Copyright

This project was primarily generated using artificial intelligence (AI).

No formal open-source license is currently provided.

Under applicable copyright law, purely AI-generated source code may not qualify for copyright protection. The copyright status of individual portions of this project, including any human-authored contributions, may depend on the circumstances of their creation.

No claim of exclusive authorship or copyright ownership over the AI-generated portions of this project is made.

Third-party components, dependencies and other externally sourced materials remain subject to their respective licenses and rights.

This repository is published for transparency, experimentation and educational purposes. Users are responsible for checking applicable legal requirements and third-party rights before redistributing or incorporating its contents into other projects.