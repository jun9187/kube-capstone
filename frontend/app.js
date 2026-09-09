const TOKEN_KEY = "hotel_token";

const authForms = document.getElementById("auth-forms");
const appSection = document.getElementById("app");
const logoutBtn = document.getElementById("logout-btn");
const loginForm = document.getElementById("login-form");
const registerForm = document.getElementById("register-form");
const searchForm = document.getElementById("search-form");
const hotelSelect = document.getElementById("hotel-select");
const roomsList = document.getElementById("rooms");
const bookingsList = document.getElementById("bookings");
const errorBox = document.getElementById("error");

let pollHandle = null;
let lastSearch = null; // { check_in, check_out }

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
}

function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

function showError(message) {
  errorBox.textContent = message || "";
}

async function api(path, options = {}) {
  const token = getToken();
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(path, { ...options, headers });
  if (res.status === 401) {
    logout();
    throw new Error("session expired, please log in again");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `${path} -> ${res.status}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

async function loadHotels() {
  const hotels = await api("/api/hotels");
  hotelSelect.innerHTML = hotels
    .map((h) => `<option value="${h.id}">${escapeHtml(h.name)} (${escapeHtml(h.city)})</option>`)
    .join("");
}

function renderRooms(rooms) {
  if (rooms.length === 0) {
    roomsList.innerHTML = "<li>No rooms available for those dates.</li>";
    return;
  }
  roomsList.innerHTML = rooms
    .map(
      (r) => `
      <li data-room-id="${r.id}">
        <div>
          <span class="room-type">${escapeHtml(r.room_type)} (room ${escapeHtml(r.room_number)})</span>
          <span class="price">$${r.price_per_night} / night &middot; sleeps ${r.capacity}</span>
        </div>
        <button class="book" data-room-id="${r.id}">Book</button>
      </li>`
    )
    .join("");
}

function renderBookings(bookings) {
  if (bookings.length === 0) {
    bookingsList.innerHTML = "<li>No bookings yet.</li>";
    return;
  }
  bookingsList.innerHTML = bookings
    .map(
      (b) => `
      <li data-id="${b.id}">
        <div>
          <span class="hotel-name">${escapeHtml(b.hotel_name)}</span> &mdash;
          ${escapeHtml(b.room_type)} (room ${escapeHtml(b.room_number)})
          <span class="dates">${b.check_in} &rarr; ${b.check_out} &middot; $${b.total_price}</span>
        </div>
        <span class="status ${b.status}">${b.status}</span>
        ${
          b.status === "pending" || b.status === "confirmed"
            ? `<button class="cancel" data-id="${b.id}" title="Cancel">&times;</button>`
            : ""
        }
      </li>`
    )
    .join("");
}

async function loadBookings() {
  try {
    const bookings = await api("/api/bookings");
    renderBookings(bookings);
  } catch (err) {
    showError(`Failed to load bookings: ${err.message}`);
  }
}

function showApp() {
  authForms.hidden = true;
  appSection.hidden = false;
  logoutBtn.hidden = false;
  loadHotels().catch((err) => showError(`Failed to load hotels: ${err.message}`));
  loadBookings();
  if (pollHandle) clearInterval(pollHandle);
  pollHandle = setInterval(loadBookings, 3000);
}

function logout() {
  clearToken();
  if (pollHandle) clearInterval(pollHandle);
  authForms.hidden = false;
  appSection.hidden = true;
  logoutBtn.hidden = true;
}

loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const data = new FormData(loginForm);
  try {
    const { access_token } = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email: data.get("email"), password: data.get("password") }),
    });
    setToken(access_token);
    loginForm.reset();
    showApp();
  } catch (err) {
    showError(`Login failed: ${err.message}`);
  }
});

registerForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const data = new FormData(registerForm);
  try {
    const { access_token } = await api("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({ email: data.get("email"), password: data.get("password") }),
    });
    setToken(access_token);
    registerForm.reset();
    showApp();
  } catch (err) {
    showError(`Registration failed: ${err.message}`);
  }
});

searchForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const data = new FormData(searchForm);
  const hotelId = data.get("hotel_id");
  const checkIn = data.get("check_in");
  const checkOut = data.get("check_out");
  try {
    const params = new URLSearchParams({ check_in: checkIn, check_out: checkOut });
    const rooms = await api(`/api/hotels/${hotelId}/rooms?${params}`);
    lastSearch = { check_in: checkIn, check_out: checkOut };
    renderRooms(rooms);
    showError("");
  } catch (err) {
    showError(`Search failed: ${err.message}`);
  }
});

roomsList.addEventListener("click", async (e) => {
  if (!e.target.matches("button.book")) return;
  if (!lastSearch) return;
  const roomId = e.target.dataset.roomId;
  try {
    await api("/api/bookings", {
      method: "POST",
      body: JSON.stringify({
        room_id: Number(roomId),
        check_in: lastSearch.check_in,
        check_out: lastSearch.check_out,
      }),
    });
    showError("");
    await loadBookings();
    searchForm.requestSubmit();
  } catch (err) {
    showError(`Booking failed: ${err.message}`);
  }
});

bookingsList.addEventListener("click", async (e) => {
  if (!e.target.matches("button.cancel")) return;
  const id = e.target.dataset.id;
  try {
    await api(`/api/bookings/${id}`, { method: "DELETE" });
    await loadBookings();
  } catch (err) {
    showError(`Failed to cancel booking: ${err.message}`);
  }
});

logoutBtn.addEventListener("click", logout);

if (getToken()) {
  showApp();
}
