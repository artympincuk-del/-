const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

// Physical European roulette wheel sequence (single zero), used only for
// the visual layout — the actual winning number always comes from the server.
const WHEEL_ORDER = [
  0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23, 10, 5,
  24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26,
];
const RED_NUMBERS = new Set([1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]);
const SECTOR_DEG = 360 / WHEEL_ORDER.length;

function colorOf(n) {
  if (n === 0) return "green";
  return RED_NUMBERS.has(n) ? "red" : "black";
}

function colorHex(c) {
  return { red: "#d1373f", black: "#1c1c22", green: "#2f9e52" }[c];
}

function drawWheel() {
  const canvas = document.getElementById("wheel");
  const ctx = canvas.getContext("2d");
  const size = canvas.width;
  const cx = size / 2;
  const cy = size / 2;
  const r = size / 2 - 4;
  const sectorRad = (2 * Math.PI) / WHEEL_ORDER.length;

  ctx.clearRect(0, 0, size, size);

  WHEEL_ORDER.forEach((num, i) => {
    const startAngle = -Math.PI / 2 + i * sectorRad - sectorRad / 2;
    const endAngle = startAngle + sectorRad;

    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, r, startAngle, endAngle);
    ctx.closePath();
    ctx.fillStyle = colorHex(colorOf(num));
    ctx.fill();
    ctx.strokeStyle = "rgba(0,0,0,0.35)";
    ctx.lineWidth = 1;
    ctx.stroke();

    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(startAngle + sectorRad / 2);
    ctx.translate(r * 0.82, 0);
    ctx.rotate(Math.PI / 2);
    ctx.fillStyle = "#fff";
    ctx.font = "bold 20px sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(String(num), 0, 0);
    ctx.restore();
  });
}

let currentBalance = 0;
let currentRotation = 0;
let selected = { type: null, value: null };

const balanceEl = document.getElementById("balance");
const resultEl = document.getElementById("result");
const errorEl = document.getElementById("error");
const amountInput = document.getElementById("amount");
const numberInputWrap = document.getElementById("number-input");
const numberValueInput = document.getElementById("number-value");
const spinBtn = document.getElementById("spin-btn");

function showError(msg) {
  errorEl.textContent = msg;
}
function hideError() {
  errorEl.textContent = "";
}

function setBalance(value) {
  currentBalance = value;
  balanceEl.textContent = value;
}

document.querySelectorAll(".bet-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".bet-btn").forEach((b) => b.classList.remove("selected"));
    btn.classList.add("selected");
    selected = { type: btn.dataset.type, value: btn.dataset.value };
    numberInputWrap.classList.toggle("visible", selected.type === "number");
  });
});

document.querySelectorAll(".chip").forEach((btn) => {
  btn.addEventListener("click", () => {
    amountInput.value = btn.dataset.amount === "max" ? currentBalance : btn.dataset.amount;
  });
});

async function loadState() {
  if (!tg.initData) {
    showError("Откройте это приложение через кнопку в Telegram-боте");
    return;
  }
  try {
    const res = await fetch("/api/state", { headers: { "X-Init-Data": tg.initData } });
    if (!res.ok) {
      showError("Не удалось авторизоваться");
      return;
    }
    const data = await res.json();
    setBalance(data.balance);
  } catch (e) {
    showError("Сеть недоступна");
  }
}

function spinTo(number, onDone) {
  const idx = WHEEL_ORDER.indexOf(number);
  const theta = idx * SECTOR_DEG;
  const targetMod = ((-theta % 360) + 360) % 360;
  const currentMod = ((currentRotation % 360) + 360) % 360;
  const delta = ((targetMod - currentMod) % 360 + 360) % 360;
  const extraSpins = 5;

  currentRotation += extraSpins * 360 + delta;
  document.getElementById("wheel").style.transform = `rotate(${currentRotation}deg)`;
  setTimeout(onDone, 4600);
}

spinBtn.addEventListener("click", async () => {
  hideError();

  if (!selected.type) {
    showError("Выберите тип ставки");
    return;
  }

  let betValue = selected.value;
  if (selected.type === "number") {
    const v = numberValueInput.value;
    if (v === "" || Number(v) < 0 || Number(v) > 36) {
      showError("Введите число от 0 до 36");
      return;
    }
    betValue = String(Number(v));
  }

  const amount = Number(amountInput.value);
  if (!amount || amount <= 0) {
    showError("Некорректная ставка");
    return;
  }
  if (amount > currentBalance) {
    showError("Недостаточно фишек");
    return;
  }

  spinBtn.disabled = true;
  resultEl.textContent = "";
  resultEl.className = "result";

  try {
    const res = await fetch("/api/spin", {
      method: "POST",
      headers: { "X-Init-Data": tg.initData, "Content-Type": "application/json" },
      body: JSON.stringify({ amount, bet_type: selected.type, bet_value: betValue }),
    });
    const data = await res.json();

    if (!res.ok) {
      showError("Не удалось сделать ставку");
      if (typeof data.balance === "number") setBalance(data.balance);
      spinBtn.disabled = false;
      return;
    }

    spinTo(data.number, () => {
      setBalance(data.balance);
      if (data.win) {
        resultEl.textContent = `🎉 Выпало ${data.number}. Выигрыш +${data.profit}`;
        resultEl.className = "result win";
        tg.HapticFeedback?.notificationOccurred("success");
      } else {
        resultEl.textContent = `Выпало ${data.number}. Проигрыш -${amount}`;
        resultEl.className = "result lose";
        tg.HapticFeedback?.notificationOccurred("error");
      }
      spinBtn.disabled = false;
    });
  } catch (e) {
    showError("Сеть недоступна");
    spinBtn.disabled = false;
  }
});

drawWheel();
loadState();
