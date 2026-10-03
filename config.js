// On your PC (localhost) the pages keep using the local API; anywhere else they use the Render API.
window.API_BASE_URL = ["127.0.0.1", "localhost"].includes(window.location.hostname)
  ? ""
  : "https://moi-sei-api.onrender.com";
