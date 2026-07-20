import { Component, inject } from '@angular/core';
import { RouterLink } from '@angular/router';
import { KeycloakService }  from '../../core/keycloak.service';

@Component({
  selector: 'app-home',
  standalone: true,
  imports: [RouterLink],
  templateUrl: './home.component.html',
  styleUrl: './home.component.scss',
})
export class HomeComponent {
  private kc = inject(KeycloakService);

  get username(): string  { return this.kc.username; }
  get email(): string     { return this.kc.email; }
  get groups(): string[]  { return this.kc.groups; }
}
