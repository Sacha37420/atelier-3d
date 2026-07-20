import { Component, inject, OnInit, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { ApiService } from '../../core/api.service';

interface Department {
  id: number; name: string; description: string; member_count: number;
}

interface UserRecord {
  email: string; display_name: string;
  department: Pick<Department, 'id' | 'name'> | null;
  registered_at: string;
}

interface MeResponse {
  email: string; username: string; groups: string[];
  display_name: string; department: Department | null;
  registered_at: string; is_new: boolean;
}

@Component({
  selector: 'app-profile',
  standalone: true,
  imports: [DatePipe],
  templateUrl: './profile.component.html',
  styleUrl: './profile.component.scss',
})
export class ProfileComponent implements OnInit {
  private api = inject(ApiService);

  me      = signal<MeResponse | null>(null);
  users   = signal<UserRecord[]>([]);
  loading = signal(true);
  error   = signal<string | null>(null);

  ngOnInit(): void {
    this.api.getMe().subscribe({
      next: (data) => {
        this.me.set(data as MeResponse);
        this.loading.set(false);
        this.loadUsers();
      },
      error: (err) => {
        this.loading.set(false);
        this.error.set(`Impossible de charger le profil (${err.status ?? 'réseau'})`);
      },
    });
  }

  private loadUsers(): void {
    this.api.getUsers().subscribe({
      next: (users) => { this.users.set(users as UserRecord[]); },
    });
  }
}
