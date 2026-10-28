-- ==========================================================
-- SCRIPT DE INICIALIZACIÓN DE BASE DE DATOS (MySQL 8.0)
-- Proyecto: Vuelve a Ti - Psicología & Reconstrucción Personal
-- ==========================================================

CREATE DATABASE IF NOT EXISTS `psicologia`
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

-- Creación de usuario y asignación de permisos (ejecutar como root)
CREATE USER IF NOT EXISTS 'psicologiauser'@'%' IDENTIFIED BY 'Psicologiapassword123.';
CREATE USER IF NOT EXISTS 'psicologiauser'@'localhost' IDENTIFIED BY 'Psicologiapassword123.';
GRANT ALL PRIVILEGES ON `psicologia`.* TO 'psicologiauser'@'%';
GRANT ALL PRIVILEGES ON `psicologia`.* TO 'psicologiauser'@'localhost';
FLUSH PRIVILEGES;

USE `psicologia`;

-- 1. Tabla de Usuarios (admin y alumnos)
CREATE TABLE IF NOT EXISTS `users` (
  `id` INT AUTO_INCREMENT PRIMARY KEY,
  `name` VARCHAR(120) NULL DEFAULT 'Alumno/a',
  `email` VARCHAR(150) NOT NULL UNIQUE,
  `password_hash` VARCHAR(255) NOT NULL,
  `role` ENUM('admin', 'user') NOT NULL DEFAULT 'user',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `is_active` TINYINT(1) NOT NULL DEFAULT 1
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 2. Tabla de Configuración del Sistema (SMTP, Gmail API y Cloudflare R2 / S3)
CREATE TABLE IF NOT EXISTS `system_config` (
  `id` INT AUTO_INCREMENT PRIMARY KEY,
  `smtp_host` VARCHAR(150) DEFAULT 'smtp.gmail.com',
  `smtp_port` INT DEFAULT 587,
  `smtp_user` VARCHAR(150) DEFAULT '',
  `smtp_password` VARCHAR(255) DEFAULT '',
  `use_gmail_api` TINYINT(1) NOT NULL DEFAULT 0,
  `gmail_token_json` LONGTEXT NULL,
  `s3_enabled` TINYINT(1) NOT NULL DEFAULT 0,
  `s3_provider` VARCHAR(50) DEFAULT 'cloudflare_r2',
  `s3_endpoint_url` VARCHAR(255) DEFAULT '',
  `s3_region` VARCHAR(80) DEFAULT 'auto',
  `s3_bucket_name` VARCHAR(150) DEFAULT '',
  `s3_access_key_id` VARCHAR(255) DEFAULT '',
  `s3_secret_access_key` VARCHAR(255) DEFAULT '',
  `presigned_url_expiration` INT DEFAULT 1800
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 3. Tabla de Mensajes de Contacto desde la Landing Page
CREATE TABLE IF NOT EXISTS `contact_messages` (
  `id` INT AUTO_INCREMENT PRIMARY KEY,
  `name` VARCHAR(120) NOT NULL,
  `email` VARCHAR(150) NOT NULL,
  `phone` VARCHAR(50) NULL,
  `message` TEXT NOT NULL,
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 4. Tabla de Cursos (soporta 1 o múltiples cursos con cronograma configurable)
CREATE TABLE IF NOT EXISTS `courses` (
  `id` INT AUTO_INCREMENT PRIMARY KEY,
  `title` VARCHAR(200) NOT NULL,
  `slug` VARCHAR(200) NOT NULL UNIQUE,
  `subtitle` VARCHAR(350) NULL,
  `description` TEXT NULL,
  `is_active` TINYINT(1) NOT NULL DEFAULT 1,
  -- Modos de cronograma:
  -- 'weekly_days': Clase 1 el día de inscripción/pago, siguientes clases cada día(s) de la semana configurado(s) (ej. 0=Lunes)
  -- 'interval_days': Clase 1 el día de inscripción/pago, siguientes clases cada X días
  -- 'immediate': Todas las clases activas desde el primer día
  `drip_mode` VARCHAR(40) NOT NULL DEFAULT 'weekly_days',
  `drip_weekdays` VARCHAR(50) NOT NULL DEFAULT '0',
  `drip_interval_days` INT NOT NULL DEFAULT 7,
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 5. Tabla de Módulos de Curso
CREATE TABLE IF NOT EXISTS `course_modules` (
  `id` INT AUTO_INCREMENT PRIMARY KEY,
  `course_id` INT NOT NULL,
  `title` VARCHAR(200) NOT NULL,
  `subtitle` TEXT NULL,
  `order_index` INT NOT NULL DEFAULT 1,
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT `fk_module_course` FOREIGN KEY (`course_id`) REFERENCES `courses`(`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 6. Tabla de Clases (Lessons) dentro de cada Módulo
CREATE TABLE IF NOT EXISTS `lessons` (
  `id` INT AUTO_INCREMENT PRIMARY KEY,
  `module_id` INT NOT NULL,
  `title` VARCHAR(200) NOT NULL,
  `description` TEXT NULL,
  `order_index` INT NOT NULL DEFAULT 1,
  `has_video` TINYINT(1) NOT NULL DEFAULT 0,
  `video_storage_provider` VARCHAR(30) NOT NULL DEFAULT 's3',
  `video_object_key` VARCHAR(500) NULL,
  `video_original_name` VARCHAR(255) NULL,
  `video_mime_type` VARCHAR(100) NULL DEFAULT 'video/mp4',
  `video_size_bytes` BIGINT NOT NULL DEFAULT 0,
  `video_duration_seconds` INT NOT NULL DEFAULT 0,
  `booking_enabled` TINYINT(1) NOT NULL DEFAULT 0,
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT `fk_lesson_module` FOREIGN KEY (`module_id`) REFERENCES `course_modules`(`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 7. Tabla de Recursos de cada Clase (PDF, Word, Presentación, Audio, etc.)
CREATE TABLE IF NOT EXISTS `lesson_resources` (
  `id` INT AUTO_INCREMENT PRIMARY KEY,
  `lesson_id` INT NOT NULL,
  `title` VARCHAR(200) NOT NULL,
  `resource_type` VARCHAR(50) NOT NULL DEFAULT 'pdf',
  `storage_provider` VARCHAR(30) NOT NULL DEFAULT 's3',
  `object_key` VARCHAR(500) NOT NULL,
  `original_filename` VARCHAR(255) NULL,
  `mime_type` VARCHAR(120) NULL,
  `file_size_bytes` BIGINT NOT NULL DEFAULT 0,
  `order_index` INT NOT NULL DEFAULT 1,
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT `fk_resource_lesson` FOREIGN KEY (`lesson_id`) REFERENCES `lessons`(`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 8. Tabla de Inscripciones / Matrículas de Alumnos en Cursos
CREATE TABLE IF NOT EXISTS `enrollments` (
  `id` INT AUTO_INCREMENT PRIMARY KEY,
  `user_id` INT NOT NULL,
  `course_id` INT NOT NULL,
  `enrolled_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `status` VARCHAR(30) NOT NULL DEFAULT 'active',
  `unlock_all_override` TINYINT(1) NOT NULL DEFAULT 0,
  UNIQUE KEY `uq_user_course` (`user_id`, `course_id`),
  CONSTRAINT `fk_enrollment_user` FOREIGN KEY (`user_id`) REFERENCES `users`(`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_enrollment_course` FOREIGN KEY (`course_id`) REFERENCES `courses`(`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 9. Tabla de Desbloqueos / Bloqueos Manuales por el Administrador
CREATE TABLE IF NOT EXISTS `student_lesson_overrides` (
  `id` INT AUTO_INCREMENT PRIMARY KEY,
  `user_id` INT NOT NULL,
  `lesson_id` INT NOT NULL,
  `force_state` VARCHAR(20) NOT NULL DEFAULT 'unlocked',
  `note` VARCHAR(255) NULL,
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY `uq_override_user_lesson` (`user_id`, `lesson_id`),
  CONSTRAINT `fk_override_user` FOREIGN KEY (`user_id`) REFERENCES `users`(`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_override_lesson` FOREIGN KEY (`lesson_id`) REFERENCES `lessons`(`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 10. Tabla de Progreso de Clases y Vídeos por Alumno
CREATE TABLE IF NOT EXISTS `student_lesson_progress` (
  `id` INT AUTO_INCREMENT PRIMARY KEY,
  `user_id` INT NOT NULL,
  `lesson_id` INT NOT NULL,
  `watched_seconds` DOUBLE NOT NULL DEFAULT 0,
  `max_watched_seconds` DOUBLE NOT NULL DEFAULT 0,
  `video_duration_seconds` DOUBLE NOT NULL DEFAULT 0,
  `video_progress_percent` DOUBLE NOT NULL DEFAULT 0,
  `lesson_progress_percent` DOUBLE NOT NULL DEFAULT 0,
  `is_completed` TINYINT(1) NOT NULL DEFAULT 0,
  `completed_at` DATETIME NULL,
  `last_accessed_at` DATETIME NULL,
  UNIQUE KEY `uq_progress_user_lesson` (`user_id`, `lesson_id`),
  CONSTRAINT `fk_progress_user` FOREIGN KEY (`user_id`) REFERENCES `users`(`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_progress_lesson` FOREIGN KEY (`lesson_id`) REFERENCES `lessons`(`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 11. Tabla de Registro de Descargas de Recursos por Alumno
CREATE TABLE IF NOT EXISTS `resource_download_logs` (
  `id` INT AUTO_INCREMENT PRIMARY KEY,
  `user_id` INT NOT NULL,
  `resource_id` INT NOT NULL,
  `lesson_id` INT NOT NULL,
  `download_count` INT NOT NULL DEFAULT 1,
  `first_downloaded_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `last_downloaded_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY `uq_download_user_resource` (`user_id`, `resource_id`),
  CONSTRAINT `fk_download_user` FOREIGN KEY (`user_id`) REFERENCES `users`(`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_download_resource` FOREIGN KEY (`resource_id`) REFERENCES `lesson_resources`(`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_download_lesson` FOREIGN KEY (`lesson_id`) REFERENCES `lessons`(`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 12. Tabla de Reservas de Sesiones 1:1 con la Psicóloga
CREATE TABLE IF NOT EXISTS `session_bookings` (
  `id` INT AUTO_INCREMENT PRIMARY KEY,
  `user_id` INT NOT NULL,
  `lesson_id` INT NOT NULL,
  `course_id` INT NULL,
  `start_at` DATETIME NOT NULL,
  `end_at` DATETIME NOT NULL,
  `status` VARCHAR(20) NOT NULL DEFAULT 'confirmed',
  `google_event_id` VARCHAR(255) NULL DEFAULT '',
  `meet_link` VARCHAR(500) NULL DEFAULT '',
  `calendar_synced` TINYINT(1) NOT NULL DEFAULT 0,
  `cancelled_by` VARCHAR(20) NULL,
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `cancelled_at` DATETIME NULL,
  INDEX `idx_booking_start` (`start_at`),
  INDEX `idx_booking_status` (`status`),
  CONSTRAINT `fk_booking_user` FOREIGN KEY (`user_id`) REFERENCES `users`(`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_booking_lesson` FOREIGN KEY (`lesson_id`) REFERENCES `lessons`(`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_booking_course` FOREIGN KEY (`course_id`) REFERENCES `courses`(`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


