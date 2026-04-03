$(document).ready(function() {
    $("#logout").click(function(e) {
        e.preventDefault();
        $.ajax({
            url: "/logout",
            type: "POST",
            success: function(data) {
                window.location.href = "/";
            },
            error: function() {
                // AJAX 요청이 실패한 경우 처리할 로직
            }
        });
    });
});