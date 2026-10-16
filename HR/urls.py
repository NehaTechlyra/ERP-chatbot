from django.urls import path
from . import views 
from . import attendance_views

urlpatterns = [
    path('employee_register/',views.employee_register,name='employee_register'),
    path('sidebar/',views.sidebar,name='sidebar'),
    path('',views.hr_dashboard,name='hr_dashboard'),
    # Recruit landing page
    # This URL routes '/recruit/' to the recruit_dashboard view (scaffolded UI).
    # Later: when we add a separate `recruit` app or split views, update this
    # pattern to point to the new app's urls (e.g., include('recruit.urls')).
    # path('recruit/', views.recruit_dashboard, name='recruit_dashboard'),
    path('recruiter-dashboard/',views.recruit_dashboard,name='recruiter_dashboard'),
    path('job-creation/',views.job_creation,name='job_creation'),
    path('jobs/', views.job_list, name='job_list'),
    path('jobs/<int:pk>/', views.job_detail, name='job_detail'),
    path('jobs/<int:pk>/edit/', views.job_edit, name='job_edit'),
    path('jobs/<int:pk>/delete/', views.job_delete, name='job_delete'),
    path('candidates/', views.candidate_list, name='candidate_list'),
    path('candidates/<int:pk>/', views.candidate_detail, name='candidate_detail'),
    path('candidates/add/', views.candidate_create, name='candidate_create'),
    path('candidates/<int:pk>/edit/', views.candidate_edit, name='candidate_edit'),
    path('candidates/<int:pk>/delete/', views.candidate_delete, name='candidate_delete'),
    path('candidates/<int:pk>/convert/', views.candidate_convert_to_employee, name='candidate_convert_to_employee'),
    path('offers/', views.offer_list, name='offer_list'),
    path('offers/add/', views.offer_create, name='offer_create'),
    path('offers/<int:pk>/', views.offer_detail, name='offer_detail'),
    path('offers/<int:pk>/edit/', views.offer_edit, name='offer_edit'),
    path('offers/<int:pk>/delete/', views.offer_delete, name='offer_delete'),
    path('offers/<int:pk>/send/', views.offer_send_email, name='offer_send_email'),
    path('offers/<int:pk>/status/', views.offer_update_status, name='offer_update_status'),
    path('offers/<int:pk>/convert/', views.offer_convert_to_employee, name='offer_convert_to_employee'),
    path('reports/', views.recruitment_reports, name='recruitment_reports'),
    path('admin-dashboard/', views.admin_dashboard, name='admin_dashboard'),
    # AJAX endpoints used by the employee register form to create Department/Designation
    # Keep these under the HR app for now; consider moving to department/designation
    # app urls if you split responsibilities later.
    path('ajax/create-department/', views.ajax_create_department, name='ajax_create_department'),
    path('ajax/create-designation/', views.ajax_create_designation, name='ajax_create_designation'),
    path('ajax/create-bank/', views.ajax_create_bank, name='ajax_create_bank'),
    path('ajax/create-allowance/', views.ajax_create_allowance, name='ajax_create_allowance'),
    path('ajax/create-leave/', views.ajax_create_leave, name='ajax_create_leave'),
    path('ajax/load-designations/', views.load_designations, name='ajax_load_designations'),
    path('ajax/create-personal-document-type/', views.ajax_create_personal_document_type, name='ajax_create_personal_document_type_url'),
    path('ajax/open-positions-data/', views.open_positions_data, name='ajax_open_positions_data'),
    path('employee/<int:pk>/edit/', views.employee_edit, name='employee_edit'),
    path('employee/<int:pk>/delete/', views.employee_delete, name='employee_delete'),
    path('employee/<int:pk>/deactivate/', views.employee_deactivate, name='employee_deactivate'),
    path('employees/', views.employees_list, name='employees_list'),

    # Attendance
    path('attendance/', attendance_views.attendance_list, name='hr_attendance_list'),
    path('attendance/monthly/', attendance_views.attendance_monthly, name='hr_attendance_monthly'),
    path('attendance/check-in/', attendance_views.attendance_punch, {'action': 'check-in'}, name='hr_attendance_check_in'),
    path('attendance/check-out/', attendance_views.attendance_punch, {'action': 'check-out'}, name='hr_attendance_check_out'),

    # Leave requests & balances
    path('leave-requests/', attendance_views.leave_request_list, name='hr_leave_request_list'),
    path('leave-requests/add/', attendance_views.leave_request_create, name='hr_leave_request_create'),
    path('leave-requests/<int:pk>/approve/', attendance_views.leave_request_action, {'action': 'approve'}, name='hr_leave_request_approve'),
    path('leave-requests/<int:pk>/reject/', attendance_views.leave_request_action, {'action': 'reject'}, name='hr_leave_request_reject'),
    path('leave-requests/<int:pk>/cancel/', attendance_views.leave_request_action, {'action': 'cancel'}, name='hr_leave_request_cancel'),
    path('leave-balances/', attendance_views.leave_balance_list, name='hr_leave_balance_list'),

    path('notifications/send-message/<int:document_id>/', views.send_message, name='send_message'),
    path('notifications/mark-read/<int:notif_id>/', views.mark_notification_read, name='mark_notification_read'),
    path('notifications/mark-unread/<int:notif_id>/', views.mark_notification_unread, name='mark_notification_unread'),
    path('notifications/mark-all-read/', views.mark_all_as_read, name='mark_all_as_read'),
    path('notifications/', views.all_notifications, name='all_notifications'),

    path('notifications/send-message/<int:document_id>/', views.send_message, name='send_message'),
    

    path('notifications/delete/<int:notif_id>/', views.delete_notification, name='delete_notification'),
    path('notifications/refresh/', views.refresh_notifications, name='refresh_notifications'),

    

    
]
