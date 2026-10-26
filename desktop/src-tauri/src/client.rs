//! Talks to the Trailmix backend: the same REST API and WebSockets the web app and the Mac app use
//! (ServerClient.swift).

use std::fmt;
use std::net::TcpStream;
use std::time::Duration;

use serde::Deserialize;
use serde_json::Value;
use tungstenite::client::IntoClientRequest;
use tungstenite::http::HeaderValue;
use tungstenite::stream::MaybeTlsStream;
use tungstenite::WebSocket;

/// A recording in progress on the server, whichever app is capturing it (see GET /api/live).
#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct LiveRecording {
    pub id: i64,
    #[serde(default)]
    pub title: String,
    /// "web" (a Trailmix window) or "helper" (a native recorder like this one).
    #[serde(default)]
    pub client: String,
    #[serde(default)]
    pub duration: f64,
    #[serde(default)]
    pub has_system: bool,
    #[serde(default)]
    pub drafts: Vec<DraftLine>,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct DraftLine {
    #[serde(default)]
    pub start: f64,
    pub speaker: Option<String>,
    #[serde(default)]
    pub text: String,
}

#[derive(Clone, Debug, PartialEq)]
pub enum ServerError {
    Offline,
    NeedsToken,
    /// A Trailmix server from before the recorder existed.
    Outdated,
    Rejected(String),
}

impl fmt::Display for ServerError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ServerError::Offline => write!(f, "Trailmix isn't running."),
            ServerError::NeedsToken => write!(f, "This Trailmix needs an access token. Add it in Settings."),
            ServerError::Outdated => write!(f, "Trailmix's server is older than this app. Restart it to finish updating."),
            ServerError::Rejected(detail) => write!(f, "{detail}"),
        }
    }
}

pub type Socket = WebSocket<MaybeTlsStream<TcpStream>>;

#[derive(Clone)]
pub struct Client {
    /// "http://127.0.0.1:8765", no trailing slash.
    pub base: String,
    token: String,
}

impl Client {
    pub fn new(base: &str, token: &str) -> Client {
        Client { base: base.trim_end_matches('/').to_string(), token: token.to_string() }
    }

    fn call(&self, method: &str, path: &str, json: Option<&Value>, timeout: u64) -> Result<minreq::Response, ServerError> {
        let url = format!("{}/api/{path}", self.base);
        let mut request = match method {
            "POST" => minreq::post(&url),
            "PUT" => minreq::put(&url),
            _ => minreq::get(&url),
        }
        .with_timeout(timeout);
        if !self.token.is_empty() {
            request = request.with_header("Authorization", format!("Bearer {}", self.token));
        }
        if let Some(body) = json {
            request = request.with_json(body).map_err(|e| ServerError::Rejected(e.to_string()))?;
        }
        let response = request.send().map_err(|_| ServerError::Offline)?;
        match response.status_code {
            200..=299 => Ok(response),
            401 => Err(ServerError::NeedsToken),
            404 | 405 => Err(ServerError::Outdated),
            status => {
                let detail = response
                    .json::<Value>()
                    .ok()
                    .and_then(|v| v.get("detail").and_then(|d| d.as_str()).map(String::from))
                    .unwrap_or_else(|| format!("Trailmix answered {status}"));
                Err(ServerError::Rejected(detail))
            }
        }
    }

    /// Reachable, and signed in if the server needs a token.
    pub fn check(&self) -> Result<(), ServerError> {
        let answer: Value = self.call("GET", "auth", None, 8)?.json().map_err(|_| ServerError::Offline)?;
        if answer.get("ok").and_then(|v| v.as_bool()) == Some(false) {
            return Err(ServerError::NeedsToken);
        }
        Ok(())
    }

    pub fn live(&self) -> Result<Vec<LiveRecording>, ServerError> {
        self.call("GET", "live", None, 8)?.json().map_err(|e| ServerError::Rejected(format!("Trailmix's answer made no sense: {e}")))
    }

    pub fn start_meeting(&self) -> Result<i64, ServerError> {
        let made: Value = self
            .call("POST", "meetings", Some(&serde_json::json!({"title": ""})), 8)?
            .json()
            .map_err(|_| ServerError::Rejected("Trailmix didn't create the meeting".into()))?;
        made.get("id").and_then(|v| v.as_i64()).ok_or_else(|| ServerError::Rejected("Trailmix didn't create the meeting".into()))
    }

    pub fn mark(&self, id: i64) -> Result<(), ServerError> {
        self.call("POST", &format!("meetings/{id}/mark"), Some(&serde_json::json!({"note": ""})), 8).map(|_| ())
    }

    pub fn stop(&self, id: i64) -> Result<(), ServerError> {
        self.call("POST", &format!("meetings/{id}/stop"), None, 8).map(|_| ())
    }

    /// Tells the server this recorder is here and what it would record; returns the commands waiting for
    /// it (the web app's Record button sends "start").
    pub fn check_in(&self, info: &Value) -> Result<Vec<String>, ServerError> {
        let answer: Value = self.call("POST", "recorder/check-in", Some(info), 8)?.json().map_err(|_| ServerError::Offline)?;
        Ok(answer
            .get("commands")
            .and_then(|c| c.as_array())
            .map(|a| a.iter().filter_map(|c| c.as_str().map(String::from)).collect())
            .unwrap_or_default())
    }

    fn socket_url(&self, path: &str) -> String {
        let ws = if self.base.starts_with("https://") { "wss" } else { "ws" };
        let rest = self.base.splitn(2, "://").nth(1).unwrap_or(&self.base);
        format!("{ws}://{rest}{path}")
    }

    fn connect(&self, path: &str) -> Result<Socket, ServerError> {
        let mut request = self.socket_url(path).into_client_request().map_err(|e| ServerError::Rejected(e.to_string()))?;
        if !self.token.is_empty() {
            if let Ok(value) = HeaderValue::from_str(&format!("Bearer {}", self.token)) {
                request.headers_mut().insert("Authorization", value);
            }
        }
        match tungstenite::connect(request) {
            Ok((socket, _)) => Ok(socket),
            Err(tungstenite::Error::Http(response)) => match response.status().as_u16() {
                401 => Err(ServerError::NeedsToken),
                404 | 405 => Err(ServerError::Outdated),
                status => Err(ServerError::Rejected(format!("Trailmix refused the connection ({status})"))),
            },
            Err(_) => Err(ServerError::Offline),
        }
    }

    /// The audio WebSocket for a meeting.
    pub fn open_stream(&self, id: i64, draft: bool) -> Result<Socket, ServerError> {
        self.connect(&format!("/api/meetings/{id}/stream?draft={}&client=helper", if draft { 1 } else { 0 }))
    }

    /// The recorder's own socket: this app sends what it would record (as for check_in) when that changes,
    /// and gets {"commands": [...]} the moment the window asks for something and {"live": [...]} whenever
    /// the recordings in progress change. A server from before it answers 404 (Outdated).
    pub fn open_recorder_socket(&self) -> Result<Socket, ServerError> {
        self.connect("/api/recorder/ws")
    }
}

/// How long a `read` waits for a message before giving up (with a benign error) so the caller can send.
pub fn set_read_timeout(socket: &mut Socket, timeout: Duration) {
    if let MaybeTlsStream::Plain(stream) = socket.get_mut() {
        let _ = stream.set_read_timeout(Some(timeout));
    }
}

/// True for the error a read returns when the timeout passed with nothing to read.
pub fn timed_out(error: &tungstenite::Error) -> bool {
    match error {
        tungstenite::Error::Io(e) => matches!(e.kind(), std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut),
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn socket_urls_follow_the_base() {
        let client = Client::new("http://127.0.0.1:8765/", "");
        assert_eq!(client.base, "http://127.0.0.1:8765");
        assert_eq!(client.socket_url("/api/recorder/ws"), "ws://127.0.0.1:8765/api/recorder/ws");
        let client = Client::new("https://trailmix.example.com", "t");
        assert_eq!(client.socket_url("/api/x"), "wss://trailmix.example.com/api/x");
    }

    #[test]
    fn live_recordings_tolerate_extra_and_missing_fields() {
        let text = r#"[{"id": 7, "title": "Sync", "client": "web", "duration": 12.5, "has_system": true, "draft": true,
                       "drafts": [{"start": 1.0, "speaker": null, "text": "hi"}], "bookmarks": [], "my_notes": []}]"#;
        let live: Vec<LiveRecording> = serde_json::from_str(text).unwrap();
        assert_eq!(live[0].id, 7);
        assert_eq!(live[0].drafts[0].text, "hi");
        let live: Vec<LiveRecording> = serde_json::from_str(r#"[{"id": 1}]"#).unwrap();
        assert_eq!(live[0].client, "");
    }
}
