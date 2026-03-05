(function() {

const chatButton = document.createElement("div");
chatButton.style.position = "fixed";
chatButton.style.bottom = "20px";
chatButton.style.right = "20px";
chatButton.style.width = "60px";
chatButton.style.height = "60px";
chatButton.style.background = "black";
chatButton.style.borderRadius = "50%";
chatButton.style.cursor = "pointer";
chatButton.style.zIndex = "9999";

document.body.appendChild(chatButton);

const chatBox = document.createElement("iframe");

const business = document.currentScript.getAttribute("data-business");

chatBox.src = "https://flask-ai-assistant-api.onrender.com/widget?business=" + business;

chatBox.style.position = "fixed";
chatBox.style.bottom = "90px";
chatBox.style.right = "20px";
chatBox.style.width = "380px";
chatBox.style.height = "520px";
chatBox.style.border = "1px solid #ccc";
chatBox.style.display = "none";
chatBox.style.zIndex = "9999";

document.body.appendChild(chatBox);

chatButton.onclick = function() {
    if(chatBox.style.display === "none"){
        chatBox.style.display = "block";
    } else {
        chatBox.style.display = "none";
    }
};

})();